"""
Abexilas Economy Hub web dashboard (aiohttp): Prices/Earnings/Stocks pages, read
APIs, and authenticated owner endpoints. Runs in its own thread (writes marshal
back to the bot loop). Set WEB_PORT in .env.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    from aiohttp import web
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False



DATA_DIR = "data"

HIDDEN_MARKET = os.environ.get("HIDDEN_MARKET_ID", "")

_STOCK_DIVIDEND_PCT = float(os.environ.get("STOCK_DIVIDEND_PCT", "0") or 0)


def _earnings_hidden_markets() -> set:
    """Market IDs whose earnings + CSN-derived prices are hidden from the PUBLIC
    dashboard, toggled live via /market hide_earnings (stored in bot_config). The
    market stays active/tradeable and owners still see everything in Discord."""
    hidden = set()
    if HIDDEN_MARKET:
        hidden.add(HIDDEN_MARKET)
    try:
        import Restocker_db as _db
        raw = _db.get_config("earnings_hidden_markets") or ""
        hidden |= {p.strip() for p in str(raw).replace(";", ",").split(",") if p.strip()}
    except Exception:
        pass
    return hidden


def _resolve_data_file(name: str) -> str:
    """Mirror of the bot's data-file resolver: map a bare filename to its
    organized location under data/ (csn_history*.yml -> data/csn_history/,
    *.csv -> data/exports/, other *.yml -> data/state/), falling back to the
    legacy working-directory path while files haven't been moved yet."""
    base = os.path.basename(str(name))
    if not base:
        return name
    if base.startswith("csn_history"):
        sub = "csn_history"
    elif base.endswith(".csv"):
        sub = "exports"
    elif base.endswith((".yml", ".yaml")):
        sub = "state"
    else:
        return name
    organized = os.path.join(DATA_DIR, sub, base)
    if os.path.exists(organized):
        return organized
    if os.path.exists(base):
        return base
    return organized


_SESSIONS: dict = {}
_LINK_ATTEMPTS: dict = {}
_REQ_HITS: dict = {}
_last_throttle_sweep: float = 0.0
_CACHE: dict = {}


def _cached(key: str, producer, ttl: float = 8.0):
    """Memoise an expensive loader for `ttl` seconds. The dashboard re-reads
    every market YAML and runs per-market DB queries on each request; without
    this an unauthenticated flood of `/` could starve the shared event loop the
    Discord bot also runs on."""
    import time as _t
    now = _t.time()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    val = producer()
    _CACHE[key] = (now + ttl, val)
    return val


def _load_sessions() -> dict:
    return _load_data_yaml("web_sessions.yml", {}) or {}


def _save_sessions(sessions: dict) -> bool:
    return _save_data_yaml("web_sessions.yml", sessions)


def _load_data_yaml(name: str, default):
    if not _YAML_AVAILABLE:
        return default
    try:
        with open(_resolve_data_file(name), encoding="utf-8") as f:
            return _yaml.safe_load(f) or default
    except FileNotFoundError:
        return default
    except Exception:
        return default


def _save_data_yaml(name: str, data) -> bool:
    if not _YAML_AVAILABLE:
        return False
    try:
        path = _resolve_data_file(name)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            _yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        return True
    except Exception:
        return False


def _session_user(request):
    """Return the {user_id,name} for the request's session cookie, or None.
    Falls back to the on-disk session store so logins survive bot restarts."""
    tok = request.cookies.get("vtm_sess")
    if not tok:
        return None
    sess = _SESSIONS.get(tok) or _load_sessions().get(tok)
    if not sess:
        return None
    # Enforce server-side expiry so a leaked/stale token can't live forever.
    # Sessions created before this field existed are treated as still valid
    # (grandfathered) rather than logging everyone out.
    exp = sess.get("expires")
    if exp is not None:
        import time as _t
        try:
            if float(exp) <= _t.time():
                _SESSIONS.pop(tok, None)
                stored = _load_sessions()
                if stored.pop(tok, None) is not None:
                    _save_sessions(stored)
                return None
        except (TypeError, ValueError):
            pass
    _SESSIONS[tok] = sess
    return sess


def _user_prefs() -> dict:
    return _load_data_yaml("web_user_prefs.yml", {}) or {}


def _market_ticker(mid: str) -> str:
    """Short stock-ticker symbol for a market (mirrors the bot)."""
    t = (_load_data_yaml("market_tickers.yml", {}) or {}).get(mid)
    if t:
        return str(t).upper()
    return ("".join(ch for ch in str(mid or "") if ch.isalnum())[:4] or "MKT").upper()


def _load_items() -> dict:
    try:
        import Restocker_db as db
        rows = db.get_items()
        # Live barrel stock (from csn_stock scans) wins over the catalog's
        # order-fulfillment counter, so the website shows real shop fullness. We also
        # keep the scanned per-unit price so items with no curated catalog price still
        # show the shop's real listed price instead of 0. Catalog items are keyed by
        # name (market "main"), while scans are per-market, so we also index price by
        # bare item name as a fallback.
        live = {}
        live_price = {}
        name_price = {}
        try:
            for _r in db.get_all_market_stock() or []:
                _k = (_r.get("market_id"), _r.get("item"))
                live[_k] = int(_r.get("stock") or 0)
                # Only per-unit rows (carrying a listing qty) are trusted for price;
                # legacy NULL-qty rows are per-bulk and skipped until re-scanned.
                _has_qty = (_r.get("sell_qty") is not None) or (_r.get("buy_qty") is not None)
                if not _has_qty:
                    continue
                _sp = _r.get("sell_price")
                if _sp is None or float(_sp) <= 0:
                    _sp = _r.get("buy_price")
                if _sp is not None and float(_sp) > 0:
                    live_price[_k] = float(_sp)
                    name_price.setdefault(_r.get("item"), float(_sp))
        except Exception:
            live = {}
            live_price = {}
            name_price = {}

        def _coin_for(name, info):
            c = info.get("coin", 0) or 0
            if c and float(c) > 0:
                return c
            mid = info.get("market_id", "main")
            return round((live_price.get((mid, name)) or name_price.get(name) or 0), 2)

        return {name: {
            "coin":      _coin_for(name, info),
            "stock":     live.get((info.get("market_id", "main"), name), info.get("stock", 0)),
            "unit_type": info.get("unit_type", "pieces"),
            "market_id": info.get("market_id", "main"),
        } for name, info in rows.items()}
    except Exception:
        pass
    if _YAML_AVAILABLE:
        try:
            with open(_resolve_data_file("items.yml"), encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
            raw = data.get("items", {}) or {}
            return {name: {
                "coin":      info.get("coin", 0) if isinstance(info, dict) else 0,
                "stock":     info.get("stock", 0) if isinstance(info, dict) else 0,
                "unit_type": info.get("unit_type", "pieces") if isinstance(info, dict) else "pieces",
                "market_id": info.get("market_id", "main") if isinstance(info, dict) else "main",
            } for name, info in raw.items() if name and info is not None}
        except Exception:
            pass
    return {}


def _load_markets() -> dict:
    try:
        import Restocker_db as db
        return db.get_markets()
    except Exception:
        pass
    if _YAML_AVAILABLE:
        try:
            with open(_resolve_data_file("markets.yml"), encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
            return data.get("markets", {}) or {}
        except Exception:
            pass
    return {}






def _market_history_file(mid: str, minfo: dict | None) -> str:
    """Resolve the CSN-history YAML filename for a market, mirroring the bot's
    own naming convention so the website reads exactly what the bot wrote."""
    configured = (minfo.get("csn_history_file") if isinstance(minfo, dict) else None)
    name = str(configured) if configured else ("csn_history.yml" if mid == "main" else f"csn_history_{mid}.yml")
    return _resolve_data_file(name)


def _load_market_prices() -> dict:
    """Derive per-market item prices from each market's CSN history (DB-backed).

    Returns {market_id: {item_name: {"coin", "sold", "bought"}}}. CSN records carry
    no catalog price, only sales aggregates, so we estimate the effective sell price
    as |net_coins| / sold_qty summed across recorded months. Curated prices from the
    items table override these in the frontend; this fills in everything else.
    """
    try:
        import Restocker_db as db
    except Exception:
        return {}

    markets = _load_markets()
    market_ids = set(markets.keys()) | {"main"}
    try:
        market_ids |= set(db.csn_all_market_ids())
    except Exception:
        pass

    # Live barrel stock per (market, item) from csn_stock scans — lets derived
    # (non-curated) rows show real fullness instead of a hardcoded 0. We also grab
    # the scanned per-unit sell/buy price: it's the shop's actual listed price and is
    # cleaner than the |net|/sold estimate, which reads 0 whenever a month's coins net
    # out. Only rows carrying a listing qty (buy_qty/sell_qty) are trusted for price —
    # those were scanned after per-unit normalization; a NULL qty means a legacy
    # per-bulk row, which we skip so it can't show 64x-high. Re-scan heals it.
    live = {}
    live_price = {}
    try:
        for _r in db.get_all_market_stock() or []:
            _k = (_r.get("market_id"), _r.get("item"))
            live[_k] = int(_r.get("stock") or 0)
            _has_qty = (_r.get("sell_qty") is not None) or (_r.get("buy_qty") is not None)
            if not _has_qty:
                continue
            _sp = _r.get("sell_price")
            if _sp is None or float(_sp) <= 0:
                _sp = _r.get("buy_price")
            if _sp is not None and float(_sp) > 0:
                live_price[_k] = float(_sp)
    except Exception:
        live = {}
        live_price = {}

    result: dict = {}
    for mid in market_ids:
        try:
            data = db.csn_get_market(mid)
        except Exception:
            continue

        agg: dict = {}
        for _mk, md in (data.get("months", {}) or {}).items():
            if not isinstance(md, dict):
                continue
            for iname, iv in (md.get("items") or {}).items():
                if not isinstance(iv, dict):
                    continue
                e = agg.setdefault(iname, {"sold": 0, "bought": 0, "net": 0.0})
                e["sold"]   += int(iv.get("sold_qty", 0) or 0)
                e["bought"] += int(iv.get("bought_qty", 0) or 0)
                e["net"]    += float(iv.get("net_coins", 0) or 0)

        priced: dict = {}
        for iname, e in agg.items():
            sold = e["sold"]
            scanned = live_price.get((mid, iname))
            if scanned:                          # real listed price beats the estimate
                coin = round(scanned, 2)         # 2dp: cheap bulk goods are <1/unit
            elif sold > 0:
                coin = max(1, round(abs(e["net"]) / sold))
            elif e["bought"] > 0:
                coin = max(1, round(abs(e["net"]) / e["bought"]))
            else:
                coin = 0
            priced[iname] = {"coin": coin, "sold": sold, "bought": e["bought"],
                             "stock": live.get((mid, iname), 0)}
        # Items that were scanned but have no CSN sales history yet still deserve a
        # row (with their listed price + live stock) instead of vanishing.
        for (_mid, _item), _px in live_price.items():
            if _mid == mid and _item not in priced:
                priced[_item] = {"coin": round(_px, 2), "sold": 0, "bought": 0,
                                 "stock": live.get((mid, _item), 0)}
        if priced:
            result[mid] = priced

    for _hid in _earnings_hidden_markets():
        result.pop(_hid, None)
    return result


def _load_earnings() -> list:
    try:
        import Restocker_db as db
        # There is no db.get_csn_history() — that call raised AttributeError on every
        # request and the bare `except` below swallowed it, so this whole DB path was
        # dead and the page always fell through to the YAML file. Real API is
        # csn_all_market_ids() + csn_get_market(mid) -> {"months": {month: {...}}}.
        totals: dict = {}
        for _mid in (db.csn_all_market_ids() or []):
            for _mk, _md in ((db.csn_get_market(_mid) or {}).get("months") or {}).items():
                if not isinstance(_md, dict):
                    continue
                t = totals.setdefault(_mk, {"month": _mk, "label": _md.get("label", _mk),
                                            "income": 0, "spent": 0, "net": 0})
                t["income"] += int(_md.get("income", 0) or 0)
                t["spent"]  += int(_md.get("spent", 0) or 0)
                t["net"]    += int(_md.get("net", 0) or 0)
        if totals:
            return [totals[k] for k in sorted(totals)]
    except Exception as _ex:
        print(f"[web] _load_earnings DB path failed, falling back to YAML: {_ex}")
    if _YAML_AVAILABLE:
        try:
            with open(_resolve_data_file("csn_history.yml"), encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
            months_raw = data.get("months", {}) or {}
            out = []
            for mk, md in sorted(months_raw.items()):
                if not isinstance(md, dict):
                    continue
                out.append({
                    "month":  mk,
                    "label":  md.get("label", mk),
                    "income": int(md.get("income", 0)),
                    "spent":  int(md.get("spent", 0)),
                    "net":    int(md.get("net", 0)),
                })
            return out
        except Exception:
            pass
    return []


def _load_earnings_full() -> dict:
    """Per-market earnings WITH per-item breakdown, for the redesigned Earnings tab.
    Shape: {"markets":[{"id","name","months":[{month,label,income,spent,net,
    items:[{item,sold,bought,net,income,expense,tsold,tbought}]}]}]}.
    Months sorted oldest→newest.
    Additive: the legacy /api/earnings endpoint is unchanged."""
    out = []
    try:
        import Restocker_db as db
        names = {}
        try:
            for mid, info in (_load_markets() or {}).items():
                names[mid] = (info.get("name") if isinstance(info, dict) else None) or mid
        except Exception:
            names = {}
        for mid in (db.csn_all_market_ids() or []):
            months = (db.csn_get_market(mid) or {}).get("months", {}) or {}
            mlist = []
            for mk in sorted(months.keys()):
                md = months[mk] or {}
                items = []
                for item, iv in (md.get("items") or {}).items():
                    if not isinstance(iv, dict):
                        continue
                    items.append({
                        "item":    item,
                        "sold":    int(iv.get("sold_qty", 0) or 0),
                        "bought":  int(iv.get("bought_qty", 0) or 0),
                        "net":     int(round(float(iv.get("net_coins", 0) or 0))),
                        "income":  int(round(float(iv.get("income_coins", 0) or 0))),
                        "expense": int(round(float(iv.get("expense_coins", 0) or 0))),
                        "tsold":   int(iv.get("times_sold", 0) or 0),
                        "tbought": int(iv.get("times_bought", 0) or 0),
                    })
                mlist.append({
                    "month":  mk,
                    "label":  md.get("label", mk),
                    "income": int(round(float(md.get("income", 0) or 0))),
                    "spent":  int(round(float(md.get("spent", 0) or 0))),
                    "net":    int(round(float(md.get("net", 0) or 0))),
                    "items":  items,
                })
            if mlist:
                out.append({"id": mid, "name": names.get(mid, mid), "months": mlist})
    except Exception as e:
        print(f"[earnings_full] {e}")
    out.sort(key=lambda m: str(m["name"]).lower())
    return {"markets": out}


def _load_stock_data() -> dict:
    """Live stock-exchange snapshot from the DB: every public market with its
    price, market cap, recent price history, change since the prior tick, and
    top holders. Read-only — the website can't trade (no per-user auth)."""
    try:
        import Restocker_db as db
        public = db.get_public_markets()
    except Exception as e:
        print(f"[stocks] DB unavailable: {e}")
        return {"markets": []}

    names = {}
    try:
        for mid, info in (_load_markets() or {}).items():
            names[mid] = (info.get("name") if isinstance(info, dict) else None) or mid
    except Exception:
        pass
    # A stock can carry a COMPANY label distinct from its host market's name (the V Tech
    # stock lives on the Greyhames market) — the exchange shows the company.
    for mid in public:
        try:
            lbl = str(db.get_config(f"stock_label:{mid}") or "").strip()
            if lbl:
                names[mid] = lbl
        except Exception:
            pass

    holder_names = {}
    if _YAML_AVAILABLE:
        try:
            with open(_resolve_data_file("stock_names.yml"), encoding="utf-8") as f:
                holder_names = _yaml.safe_load(f) or {}
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[stocks] holder names load failed: {e}")
    prefs = _user_prefs()

    def _holder_label(uid):
        uid = str(uid)
        if prefs.get(uid, {}).get("anonymous", True):
            return "…" + uid[-4:]
        return holder_names.get(uid) or ("…" + uid[-4:])

    out = []
    for mid, listing in public.items():
        try:
            price  = float(listing.get("share_price") or 0)
            shares = float(listing.get("shares_outstanding") or 0)
            pe     = float(listing.get("pe_multiplier") or 0)
            rows   = db.get_price_history(mid, limit=5000)  # deep history so the chart's 1M/1Y ranges have data
            hist   = [{"t": r.get("logged_at"), "price": float(r.get("price") or 0)}
                      for r in reversed(rows)]
            prev   = hist[-2]["price"] if len(hist) > 1 else price
            change = price - prev
            pct    = (change / prev * 100.0) if prev else 0.0
            holders = db.get_holders(mid)
            top = sorted(holders, key=lambda h: -float(h.get("shares") or 0))[:10]
            top_holders = [{
                "id":     _holder_label(h.get("user_id")),
                "shares": float(h.get("shares") or 0),
                "value":  float(h.get("shares") or 0) * price,
            } for h in top]
            div_ov = listing.get("dividend_pct")
            div_pct = float(div_ov) if div_ov is not None else _STOCK_DIVIDEND_PCT
            treasury = float(listing.get("treasury_coins") or 0)
            ld_row = db.get_last_dividend(mid) if hasattr(db, "get_last_dividend") else None
            last_div = None
            if ld_row:
                last_div = {
                    "month":     ld_row.get("month"),
                    "total":     float(ld_row.get("total_paid") or 0),
                    "per_share": float(ld_row.get("per_share") or 0),
                    "holders":   int(ld_row.get("holders") or 0),
                }
            try:
                open_orders = len(db.get_open_limit_orders(mid)) if hasattr(db, "get_open_limit_orders") else 0
            except Exception:
                open_orders = 0
            div_yield = (last_div["per_share"] / price * 100.0) if (last_div and price > 0) else 0.0
            out.append({
                "mid": mid, "name": names.get(mid, mid), "ticker": _market_ticker(mid),
                "price": price, "shares": shares, "mcap": price * shares, "pe": pe,
                "change": change, "pct": pct,
                "div_pct": div_pct, "div_yield": div_yield, "last_div": last_div,
                "treasury": treasury, "open_orders": open_orders,
                "history": hist, "holders_count": len(holders), "top_holders": top_holders,
            })
        except Exception as e:
            print(f"[stocks] failed for {mid}: {e}")
    out = [m for m in out if m["mid"] != HIDDEN_MARKET]
    out.sort(key=lambda m: -m["mcap"])
    try:
        import Restocker_db as _dbk
        _fund = float(_dbk.get_config("exchange_insurance_fund") or 0)
    except Exception:
        _dbk = None
        _fund = 0.0
    _tot_mcap = sum(m["mcap"] for m in out) or 1.0
    for _m in out:
        _assets = 0.0
        if _dbk is not None:
            try:
                # BUGFIX: count only rows the scanner stored on a per-UNIT basis (sell_qty/
                # buy_qty present). A NULL-qty row is a LEGACY per-STACK price ("64 for 2000"
                # stored raw); valuing it per-unit inflates inventory up to ~64x — the
                # "99M inventory / 383% backed / AAA" dashboard bug. Legacy rows self-heal
                # on the next fresh CSN stock scan.
                for _it, _x in (_dbk.get_market_stock(_m["mid"]) or {}).items():
                    _stk = float(_x.get("stock") or 0)
                    if _stk <= 0:
                        continue
                    if _x.get("sell_qty") is not None and _x.get("sell_price") is not None:
                        _assets += _stk * float(_x["sell_price"])
                    elif _x.get("buy_qty") is not None and _x.get("buy_price") is not None:
                        _assets += _stk * float(_x["buy_price"])
            except Exception:
                pass
        _sell = 0.0
        if _dbk is not None:
            try:
                _sell = float(_dbk.get_config(f"sellable_assets:{_m['mid']}") or 0.0)
            except Exception:
                _sell = 0.0
        _fs = _fund * (_m["mcap"] / _tot_mcap)
        _mc = _m["mcap"] or 1.0
        _m["backing_pct"] = round(100.0 * (_m["treasury"] + _assets + _sell + _fs) / _mc, 1)
        # Rating — prefer the bot's cached composite quality (backing + tp-fee traffic
        # + order flow + report history, bot_config quality:<mid>); fall back to a
        # backing-only grade if the bot hasn't computed quality yet.
        _target = (float(os.getenv("STOCK_BACK_CASH_PCT", "15") or 15)
                   + float(os.getenv("STOCK_BACK_ASSET_PCT", "25") or 25)
                   + float(os.getenv("STOCK_BACK_FUND_PCT", "10") or 10))
        _m["backing_target"] = _target
        if _dbk is not None:
            try:
                _qraw = _dbk.get_config(f"quality:{_m['mid']}")
                if _qraw:
                    _m["quality"] = json.loads(_qraw)   # kept for display (visitors etc.)
            except Exception:
                pass
        # HOUSE RULE: the grade is GATED BY COLLATERAL alone — backing % of market cap
        # against the gates A=50 / AA=60 / AAA=80 / BBB=30 / BB=15. (The old quality-ratio
        # grade mixed in traffic/orders and marked well-collateralised markets BBB.)
        _bp = float(_m["backing_pct"] or 0.0)
        _m["rating"] = ("AAA" if _bp >= 80 else "AA" if _bp >= 60 else "A" if _bp >= 50
                        else "BBB" if _bp >= 30 else "BB" if _bp >= 15 else "C")
    bonds = []
    try:
        for b in (db.list_bonds() or []):
            if b.get("status") not in ("open", "active", "defaulted"):
                continue
            cov = None
            try:
                cov = json.loads(db.get_config(f"bond_coverage:{b['market_id']}") or "null")
            except Exception:
                pass
            bonds.append({
                "id": b["id"], "name": b.get("name") or f"#{b['id']}",
                "market_id": b["market_id"], "status": b["status"],
                "coupon_pct": float(b.get("coupon_pct") or 0),
                "unit_price": float(b.get("unit_price") or 0),
                "units_left": max(0, int(float(b.get("units_total") or 0) - float(b.get("units_sold") or 0))),
                "sold_face": round(float(b.get("unit_price") or 0) * float(b.get("units_sold") or 0)),
                "matures_at": str(b.get("matures_at") or "")[:10],
                "missed": int(b.get("missed_coupons") or 0),
                "coverage": (cov or {}).get("pct"),
            })
    except Exception as e:
        print(f"[bonds] board build failed: {e}")
    index = None
    try:
        hist = db.get_market_index_history(5000)
        if hist:
            cur = float(hist[-1]["index_value"])
            prev = float(hist[-2]["index_value"]) if len(hist) > 1 else cur
            index = {
                "value": round(cur, 2),
                "change_pct": round((cur - prev) / prev * 100.0, 2) if prev else 0.0,
                "total_mcap": round(float(hist[-1]["total_mcap"])),
                "markets": int(hist[-1]["markets"]),
                "history": [{"t": h["ts"], "v": round(float(h["index_value"]), 2)} for h in hist],
            }
    except Exception:
        index = None
    return {"markets": out, "index": index, "bonds": bonds}



# _PAGE (the /classic single-page dashboard) was REMOVED 2026-07-29.
# It was 3,000+ lines duplicating Inventory / Ledger / Orders / My Market, was linked
# from no nav, and silently diverged from the real pages (a panel added there would
# never be seen). Account linking, the one thing it uniquely owned, now lives in
# _TERMINAL_NAV so every page has it. /classic redirects to /inventory.

def _jscript(obj) -> str:
    """JSON-encode for safe embedding inside an inline <script> block.
    json.dumps does NOT escape <, >, & or the JS line separators, so a value like
    "
</script>" would break out of the script context (stored XSS). Escape them."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace(" ", "\\u2028").replace(" ", "\\u2029"))


_SAFE_MARKET_FIELDS = ("name", "active", "platform_fee_pct", "ticker", "created_at")


def _public_markets(markets: dict) -> dict:
    """Strip secret/internal fields from a markets dict before exposing it."""
    out = {}
    for mid, info in (markets or {}).items():
        out[mid] = ({k: info[k] for k in _SAFE_MARKET_FIELDS if k in info}
                    if isinstance(info, dict) else info)
    return out


# ── inventory categories (auto-classified by item name; display-only, non-destructive) ──
# Groups the flat shop list into Minecraft-native buckets on the Inventory page. This does
# NOT touch items.category (the shop catalog's armor/tools/swords tags) — it's computed on
# the fly from the display name. First-match-wins; ORDER MATTERS for overlaps
# (redstone lamp→Redstone, nether wart→Farm, soul sand→Nether).
_INV_CAT_ORDER = [
    "Wood & Logs", "Ores & Minerals", "Enchanted Gear", "Redstone", "Concrete & Clay",
    "Nether", "End", "Ice & Snow", "Farm & Food", "Dyes & Wool",
    "Mob Drops", "Glass & Light", "Nature", "Building", "Other",
]
_INV_CAT_RULES = [
    # Tools/armor/weapons (the server's "enchanted gear") — matched FIRST so a
    # Diamond Pickaxe / Netherite Axe lands here, not under Ores by its material name.
    # " axe"/" hoe" use a leading space so "Waxed Copper" / "Shoe"-likes don't match.
    ("Enchanted Gear", ["pickaxe", "shovel", " axe", " hoe", "sword", "helmet",
                        "chestplate", "leggings", "boots", "elytra", "trident",
                        "crossbow", "bow", "shears", "fishing rod", "flint and steel",
                        "mace", "brush", "shield", "horse armor"]),
    ("Redstone", ["redstone", "repeater", "comparator", "piston", "observer",
                  "hopper", "dispenser", "dropper", "rail", "tripwire",
                  "daylight", "note block", "lever", "activator", "sculk sensor"]),
    ("Concrete & Clay", ["concrete", "terracotta", "glazed", "clay",
                         "mud brick", "packed mud"]),
    ("Farm & Food", ["wheat", "carrot", "potato", "beetroot", "melon", "pumpkin",
                     "apple", "bread", "seed", "sugar", "cocoa", "wart", "kelp",
                     "berry", "berries", "honey", "honeycomb", "egg", "milk",
                     "beef", "porkchop", "mutton", "chicken", "rabbit", "cod",
                     "salmon", "fish", "bamboo", "cactus", "hay", "cookie",
                     "carved pumpkin", "stew"]),
    ("Ores & Minerals", ["ingot", "ore", "raw iron", "raw copper", "raw gold",
                         "nugget", "coal", "charcoal", "lapis", "diamond",
                         "emerald", "netherite", "scrap", "amethyst",
                         "copper block", "iron block", "gold block",
                         "block of copper", "block of iron", "block of gold"]),
    ("Wood & Logs", ["log", "planks", "stem", "hyphae", "stripped", "wood"]),
    ("Ice & Snow", ["ice", "snow"]),
    ("Dyes & Wool", ["dye", "wool", "carpet", " bed", "banner"]),
    ("End", ["end stone", "ender", "chorus", "purpur", "shulker", "dragon",
             "elytra", "end rod"]),
    ("Nether", ["nether", "soul", "blaze", "ghast", "wither", "crimson",
                "warped", "magma", "glowstone", "shroomlight", "quartz",
                "blackstone", "basalt", "gilded"]),
    ("Mob Drops", ["bone", "string", "spider eye", "gunpowder", "slime",
                   "rotten flesh", "leather", "feather", "phantom", "ink sac",
                   "scute", "prismarine shard", "nautilus", "arrow", "pearl"]),
    ("Glass & Light", ["glass", "lantern", "torch", "candle", "lamp",
                       "campfire", "sea pickle"]),
    ("Nature", ["sapling", "flower", "leaves", "vine", "moss", "grass", "dirt",
                "sand", "gravel", "podzol", "mycelium", "mud", "root", "lily",
                "coral", "sponge", "mushroom", "fern", "azalea", "dripleaf",
                "spore", "lichen", "rose", "tulip", "petal"]),
    ("Building", ["stone", "cobble", "brick", "deepslate", "granite", "diorite",
                  "andesite", "tuff", "calcite", "sandstone", "prismarine",
                  "smooth", "polished", "chiseled", "slab", "stair", "wall",
                  "pillar", "tile", "mossy", "cut "]),
]


def _item_category(name: str) -> str:
    n = (name or "").lower()
    for cat, kws in _INV_CAT_RULES:
        for kw in kws:
            if kw in n:
                return cat
    return "Other"


def _load_inventory_data() -> dict:
    """Per-market barrel fullness for the Inventory tab. Merges the live barrel scan
    (stock + capacity + listed price) with the catalog, so EVERY market shows up — not just
    the barrel-scanned ones — and DERIVES a 1-barrel capacity (54 × stack) whenever a scan
    didn't store one, so fullness always renders like the markets that already work."""
    try:
        import Restocker_db as db
        import Restocker_main as m
    except Exception as e:
        print(f"[inventory] modules unavailable: {e}")
        return {"markets": []}
    names = {}
    try:
        for mid, info in (_load_markets() or {}).items():
            names[mid] = (info.get("name") if isinstance(info, dict) else None) or mid
    except Exception:
        pass
    # Catalog: every item per market, with its price + stack size (fallbacks below).
    catalog = {}
    try:
        for name, info in (db.get_items() or {}).items():
            mid = info.get("market_id") or "main"
            catalog.setdefault(mid, {})[name] = {
                "coin": float(info.get("coin", 0) or 0),
                "stack": int(info.get("stack_size", 0) or 0) or None,
            }
    except Exception:
        pass
    # Scan: stock / capacity / listed price per (market, item).
    scan = {}
    try:
        for r in (db.get_all_market_stock() or []):
            scan.setdefault(r.get("market_id") or "main", {})[r.get("item")] = r
    except Exception:
        pass

    def _cap_for(item, stack_hint):
        try:
            ss = m._detect_stack_size(item)
        except Exception:
            ss = 0
        if not ss or ss <= 0:
            ss = stack_hint or 64
        return 54 * ss                       # one full barrel = 54 slots × stack size

    out = []
    # Include every registered market (from names) too, so a market with no scan/catalog
    # items yet still shows up as an (empty) tab instead of silently vanishing.
    for mid in (set(catalog) | set(scan) | set(names)):
        cat = catalog.get(mid, {})
        sc = scan.get(mid, {})
        # The TEST fallback market self-recreates (unattributed uploads land there by
        # design, so /market delete never sticks) — hide its tab whenever it's empty.
        if str(mid).lower() == "test" and not cat and not sc:
            continue
        items = []
        for it in (set(cat) | set(sc)):
            r = sc.get(it) or {}
            cur = int(r.get("stock") or 0)
            cap = int(r.get("capacity") or 0)
            if cap <= 0:
                cap = _cap_for(it, (cat.get(it) or {}).get("stack"))
            cap = max(cap, cur)
            pct = (100.0 * cur / cap) if cap > 0 else 0.0
            _sp = r.get("sell_price")
            if _sp is None or float(_sp or 0) <= 0:
                _sp = r.get("buy_price")
            try:
                price = round(float(_sp), 2) if _sp not in (None, "") and float(_sp) > 0 else 0
            except Exception:
                price = 0
            if not price:
                price = round(float((cat.get(it) or {}).get("coin", 0) or 0), 2)
            try:
                disp = m._pretty_item_name(it)          # strips lore junk, adds curated effects
            except Exception:
                try:
                    disp = m._strip_item_code(it)
                except Exception:
                    disp = it
            disp = disp or it
            items.append({"item": disp, "stock": cur, "capacity": cap,
                          "pct": round(pct, 1), "owner": r.get("owner") or "", "price": price,
                          "cat": _item_category(disp or it)})
        items.sort(key=lambda x: x["pct"])
        low = sum(1 for x in items if x["capacity"] > 0 and x["pct"] <= 20.0)
        out.append({"market_id": mid, "name": names.get(mid, mid),
                    "items": items, "count": len(items), "low": low})
    # Markets with items first (most low-stock, then most items); empty markets last.
    out.sort(key=lambda mm: (mm["count"] == 0, -mm["low"], -mm["count"]))
    return {"markets": out}


def _load_orders_data() -> dict:
    """Open/active restock orders grouped by market, for the website Orders board (read-only).
    Shape: {"markets":[{market_id,name,count,orders:[{id,item,requested,claimed,status}]}]}."""
    try:
        import Restocker_db as db
        rows = db.load_orders()
    except Exception as e:
        print(f"[orders] DB unavailable: {e}")
        return {"markets": []}
    names = {}
    try:
        for mid, info in (_load_markets() or {}).items():
            names[mid] = (info.get("name") if isinstance(info, dict) else None) or mid
    except Exception:
        pass
    by_market = {}
    for o in rows:
        st = str(o.get("status", "") or "").lower()
        if st == "cancelled":
            continue   # cancelled are prunable junk; fulfilled are KEPT (shown at the bottom)
        mid = o.get("market_id") or "main"
        claimed = sum(int(c.get("qty") or 0) for c in (o.get("claims") or []))
        by_market.setdefault(mid, []).append({
            "id": int(o.get("id") or 0),
            "item": o.get("item") or "",
            "requested": int(o.get("requested") or 0),
            "claimed": claimed,
            "status": st or "open",
        })
    try:
        import Restocker_main as _m
    except Exception:
        _m = None
    def _ostatus_rank(x):
        # unclaimed first (0), then claimed/in-progress (1), then fulfilled at the bottom (2)
        if x["status"] == "fulfilled":
            return 2
        return 1 if x["claimed"] > 0 else 0
    out = []
    FULFILLED_SHOWN = 40   # board stays light: newest 40 fulfilled per market shown as
    for mid, orders in by_market.items():   # history; the rest still live in the DB for records
        orders.sort(key=lambda x: (_ostatus_rank(x), -x["id"]))
        active = [o for o in orders if _ostatus_rank(o) < 2]
        done = [o for o in orders if _ostatus_rank(o) == 2][:FULFILLED_SHOWN]
        orders = active + done
        loc = ""
        if _m is not None:
            try:
                loc = _m._market_sell_location(mid)
            except Exception:
                loc = ""
        out.append({"market_id": mid, "name": names.get(mid, mid),
                    "orders": orders, "count": len(active), "sell_location": loc})
    out.sort(key=lambda m: m["count"], reverse=True)
    return {"markets": out}


def _load_teams_data(days: int = 7) -> dict:
    """Cross-team performance leaderboard from the perf ledger (read-only).
    Names are resolved to in-game names; Discord IDs are never exposed."""
    try:
        import Restocker_db as db
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = db.get_all_team_perf(since)
    except Exception as e:
        print(f"[teams] DB unavailable: {e}")
        return {"teams": [], "days": days}
    # Market-owner lookup: a "sales" perf row credits a market's monthly net to its OWNER
    # (detail "vtech:2026-07"). When that owner IS the worker being ranked, it's the boss's
    # own shop revenue, not team work — showing it made the owner's test-IGN "team" rank #2
    # on pure self-credit. Exclude those rows from the leaderboard (they remain in the
    # ledger itself for the money views).
    _owners = {}
    try:
        import Restocker_main as _m_own
        for _mid, _info in (_m_own._load_markets().get("markets", {}) or {}).items():
            if isinstance(_info, dict):
                _owners[str(_mid)] = str(_info.get("owner_id") or "")
    except Exception:
        pass
    teams: dict = {}
    for r in rows:
        m = str(r["manager_id"]); k = r["kind"]
        c = float(r["coins"] or 0); q = int(r["qty"] or 0); wid = str(r["worker_id"])
        if k == "sales":
            _mid = str(r["detail"] or "").split(":", 1)[0]
            if _mid and _owners.get(_mid) == wid:
                continue                       # owner self-credit — not team performance
        t = teams.setdefault(m, {"manager_id": m, "order_coins": 0.0, "sales_coins": 0.0,
                                 "orders": 0, "futures_qty": 0, "workers": {}})
        if k == "order":
            t["order_coins"] += c; t["orders"] += 1
        elif k in ("sales", "project"):
            # "project" covers perpetual-project pay (hive harvesting, manager project pay) —
            # counted with chest-shop sales so it shows in the team totals.
            t["sales_coins"] += c
        elif k == "futures":
            t["futures_qty"] += q
        w = t["workers"].setdefault(wid, {"id": wid, "coins": 0.0})
        if k in ("order", "sales", "project"):
            w["coins"] += c
    # Include every team that has members, even with no activity yet, so new teams show up.
    try:
        for mgr in db.get_all_team_managers():
            mgr = str(mgr)
            t = teams.setdefault(mgr, {"manager_id": mgr, "order_coins": 0.0, "sales_coins": 0.0,
                                       "orders": 0, "futures_qty": 0, "workers": {}})
            for wid in db.get_team(mgr):
                t["workers"].setdefault(str(wid), {"id": str(wid), "coins": 0.0})
    except Exception as e:
        print(f"[teams] roster merge failed: {e}")
    out = []
    for m, t in teams.items():
        try:
            cap_ign = db.get_ign(m)
        except Exception:
            cap_ign = None
        workers = []
        for wid, w in t["workers"].items():
            try:
                ign = db.get_ign(wid)
            except Exception:
                ign = None
            workers.append({"ign": ign or "Worker", "coins": round(w["coins"])})
        workers.sort(key=lambda x: x["coins"], reverse=True)
        try:
            tname = (db.get_config(f"team_name:{m}") or "").strip()
        except Exception:
            tname = ""
        captain = tname or cap_ign or ((workers[0]["ign"] + "'s team") if workers else "Unnamed team")
        total = t["order_coins"] + t["sales_coins"]
        out.append({"captain": captain,
                    "members": len([w for w in t["workers"] if w != m]) + 1,  # workers + the manager, counted once
                    "orders": t["orders"], "order_coins": round(t["order_coins"]),
                    "sales_coins": round(t["sales_coins"]), "futures": t["futures_qty"],
                    "total": round(total), "top_workers": workers[:5]})
    out.sort(key=lambda x: x["total"], reverse=True)
    from datetime import datetime as _dt, timezone as _tz
    return {"teams": out, "days": days,
            "generated": _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M UTC")}


async def _handle_index(request):
    """/classic — retired. Kept as a redirect so old bookmarks and any links still
    floating around in Discord don't 404."""
    raise web.HTTPFound("/inventory")


async def _handle_api_items(request):
    return web.Response(
        text=json.dumps(_cached("items", _load_items), ensure_ascii=False),
        content_type="application/json",
    )


async def _handle_api_markets(request):
    return web.Response(
        text=json.dumps(_public_markets(_cached("markets", _load_markets)), ensure_ascii=False),
        content_type="application/json",
    )


def _earnings_hidden(market_id) -> bool:
    """Has this market opted OUT of showing its earnings publicly?

    The Ledger has no auth, so by default every market's income, spend and margin is
    visible to anyone with the URL. Some owners (viridianmarket, freezone) don't want
    that. Config key `market_earnings_public:<mid>` = "0" hides them. Default is public,
    so nothing changes for markets that never set it.
    """
    try:
        import Restocker_db as db
        return str(db.get_config(f"market_earnings_public:{market_id}") or "1").strip() == "0"
    except Exception:
        return False


def _visible_market_ids(request) -> set:
    """Market ids the CALLER may see earnings for: every public one, plus any private
    market they personally own or manage (so the owner's own view is never censored)."""
    hidden = set()
    try:
        for mid in (_load_markets() or {}).keys():
            if _earnings_hidden(mid):
                hidden.add(str(mid))
    except Exception:
        return set()
    if not hidden:
        return set()
    try:
        sess = _session_user(request)
        if sess:
            hidden -= {str(m) for m in _owner_markets_web(str(sess["user_id"]))}
    except Exception:
        pass
    return hidden


def _strip_hidden(payload, request):
    """Drop opted-out markets from an earnings payload, whatever shape it is."""
    hide = _visible_market_ids(request)
    if not hide:
        return payload
    import copy
    p = copy.deepcopy(payload)
    if isinstance(p, dict) and isinstance(p.get("markets"), list):
        p["markets"] = [m for m in p["markets"]
                        if str(m.get("id") or m.get("market_id") or "") not in hide]
    elif isinstance(p, dict):
        for k in list(p.keys()):
            if str(k) in hide:
                p.pop(k, None)
    return p


async def _handle_api_earnings(request):
    return web.Response(
        text=json.dumps(_strip_hidden(_cached("earnings", _load_earnings), request),
                        ensure_ascii=False),
        content_type="application/json",
    )


async def _handle_api_earnings_full(request):
    """Per-market earnings + per-item breakdown for the redesigned Earnings tab."""
    return web.Response(
        text=json.dumps(_strip_hidden(_cached("earnings_full", _load_earnings_full), request),
                        ensure_ascii=False),
        content_type="application/json",
    )


async def _handle_api_prices(request):
    """Per-market item prices derived from CSN history (BNL etc.)."""
    return web.Response(
        text=json.dumps(_cached("market_prices", _load_market_prices), ensure_ascii=False),
        content_type="application/json",
    )


async def _handle_api_stocks(request):
    """Live stock-exchange snapshot: public markets, prices, history, holders."""
    return web.Response(
        text=json.dumps(_cached("stock_data", _load_stock_data), ensure_ascii=False),
        content_type="application/json",
    )


async def _handle_api_link(request):
    """Validate a one-time code from /website_login and start a session."""
    import time as _t
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or (request.remote or "unknown"))
    now = _t.time()
    recent = [t for t in _LINK_ATTEMPTS.get(ip, []) if now - t < 60]
    if len(recent) >= 8:
        return web.json_response(
            {"ok": False, "error": "Too many attempts — wait a minute and try again."},
            status=429)
    recent.append(now)
    _LINK_ATTEMPTS[ip] = recent
    try:
        body = await request.json()
    except Exception:
        body = {}
    code = str(body.get("code", "")).strip().upper()
    if not code:
        return web.json_response({"ok": False, "error": "Enter your code."})
    codes = _load_data_yaml("web_login_codes.yml", {}) or {}
    entry = codes.get(code)
    if not isinstance(entry, dict) or float(entry.get("expires", 0)) <= _t.time():
        return web.json_response({"ok": False, "error": "That code is invalid or expired."})
    codes.pop(code, None)
    _save_data_yaml("web_login_codes.yml", codes)
    import secrets as _secrets
    token = _secrets.token_urlsafe(24)
    sess = {"user_id": str(entry.get("user_id")), "name": entry.get("name", ""),
            "csrf": _secrets.token_urlsafe(24),
            "expires": _t.time() + 30 * 24 * 3600}
    _SESSIONS[token] = sess
    sessions = _load_sessions()
    sessions[token] = sess
    _save_sessions(sessions)
    resp = web.json_response({"ok": True, "name": entry.get("name", "")})
    resp.set_cookie("vtm_sess", token, httponly=True, secure=True,
                    max_age=30 * 24 * 3600, samesite="Lax")
    return resp


async def _handle_api_me(request):
    """Who is logged in (from the session cookie), plus their holdings."""
    sess = _session_user(request)
    if not sess:
        return web.json_response({"logged_in": False})
    uid = str(sess["user_id"])
    # Default TRUE to match _holder_label() on the public leaderboard, which hides an
    # unset user. Reporting False here meant the UI showed "visible" for someone who was
    # actually hidden — and a toggle-on-then-off would have EXPOSED them.
    anon = bool(_user_prefs().get(uid, {}).get("anonymous", True))
    holdings = []
    try:
        import Restocker_db as db
        for h in db.get_portfolio(uid):
            mid = h.get("market_id")
            listing = db.get_market_shares(mid) or {}
            price = float(listing.get("share_price") or 0)
            shares = float(h.get("shares") or 0)
            holdings.append({
                "market": mid, "shares": shares,
                "value": shares * price, "cost": float(h.get("cost_basis") or 0),
            })
    except Exception:
        pass
    owned = []
    try:
        import Restocker_main as m
        raw = _load_markets() or {}
        for omid in m._owner_markets_for_user(uid):
            nm = (raw.get(omid, {}).get("name") if isinstance(raw.get(omid), dict) else None) or omid
            owned.append({"mid": omid, "name": nm})
    except Exception:
        pass
    csrf = sess.get("csrf")
    if not csrf:
        import secrets as _s
        csrf = _s.token_urlsafe(24)
        sess["csrf"] = csrf
        _tok = request.cookies.get("vtm_sess")
        if _tok:
            _SESSIONS[_tok] = sess
            try:
                _ss = _load_sessions(); _ss[_tok] = sess; _save_sessions(_ss)
            except Exception:
                pass
    return web.json_response({"logged_in": True, "name": sess.get("name", ""),
                              "anonymous": anon, "portfolio": holdings, "owned": owned,
                              "csrf": csrf})


async def _handle_api_anon(request):
    """Toggle the logged-in user's anonymity on the public leaderboard."""
    sess = _session_user(request)
    if not sess:
        return web.json_response({"ok": False, "error": "Not logged in."})
    # State-changing + cookie-authenticated, so it needs the token like every other
    # owner POST — otherwise another site could flip a logged-in user's visibility.
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    anon = bool(body.get("anonymous"))
    prefs = _user_prefs()
    prefs.setdefault(str(sess["user_id"]), {})["anonymous"] = anon
    _save_data_yaml("web_user_prefs.yml", prefs)
    return web.json_response({"ok": True, "anonymous": anon})


async def _handle_api_logout(request):
    tok = request.cookies.get("vtm_sess")
    if tok:
        _SESSIONS.pop(tok, None)
        sessions = _load_sessions()
        if sessions.pop(tok, None) is not None:
            _save_sessions(sessions)
    resp = web.json_response({"ok": True})
    resp.del_cookie("vtm_sess")
    return resp


async def _handle_shares(request):
    """Live cap-table / shareholder page for a market's stock: /shares/<market>[?uid=<id>].
    Shows outstanding, mktcap, ownership concentration, and the ranked holder table."""
    import Restocker_db as db
    import Restocker_main as m
    mid = (request.match_info.get("market", "") or "").strip()
    you = (request.query.get("uid") or "").strip() or None
    sh = db.get_market_shares(mid)
    if not sh:
        return web.Response(text=f"No stock listed for market '{mid}'.", status=404, content_type="text/plain")
    try:
        markets = (m._load_markets().get("markets", {}) or {})
    except Exception:
        markets = {}
    name = (markets.get(mid) or {}).get("name", mid)
    try:
        ticker = _market_ticker(mid)
    except Exception:
        ticker = mid.upper()
    holders = []
    try:
        for h in db.get_holders(mid):
            uid = str(h.get("user_id"))
            try:
                nm = db.get_ign(uid) or uid
            except Exception:
                nm = uid
            holders.append({"uid": uid, "name": nm, "shares": float(h.get("shares") or 0)})
    except Exception:
        holders = []
    lowest_ask = highest_bid = None
    try:
        orders = db.get_open_limit_orders(mid)
        asks = [float(o["limit_price"]) for o in orders if str(o.get("side")).lower() == "sell"]
        bids = [float(o["limit_price"]) for o in orders if str(o.get("side")).lower() == "buy"]
        lowest_ask = min(asks) if asks else None
        highest_bid = max(bids) if bids else None
    except Exception:
        pass
    mark = lowest_ask if lowest_ask else float(sh.get("share_price") or 0)
    try:
        html = m._render_cap_table_html(name, ticker, float(sh.get("shares_outstanding") or 0),
                                        mark, lowest_ask, highest_bid, holders, you_uid=you)
    except Exception as e:
        return web.Response(text=f"Could not render cap table: {e}", status=500, content_type="text/plain")
    return web.Response(text=html, content_type="text/html")


async def _handle_report(request):
    """Full monthly report page: /report/<market>[/<month>]. Renders the complete,
    sortable P&L (every item, income vs expense) so anyone can open and read the whole
    month. Defaults to the latest month when none is given."""
    import Restocker_db as db
    import Restocker_main as m
    mid = request.match_info.get("market", "main")
    month = request.match_info.get("month", "") or ""
    # Same opt-out as the Ledger: this page is linked straight from the Discord report
    # card, so without this check a "hidden" market's full P&L is one URL away.
    if _earnings_hidden(mid) and not _require_owner(request, mid):
        return web.Response(
            text=f"The owner of '{mid}' has made this market's earnings private.",
            status=403, content_type="text/plain")
    try:
        markets = (m._load_markets().get("markets", {}) or {})
    except Exception:
        markets = {}
    mname = (markets.get(mid) or {}).get("name", mid)
    try:
        months = (db.csn_get_market(mid) or {}).get("months", {}) or {}
    except Exception:
        months = {}
    if not months:
        return web.Response(text=f"No report data for market '{mid}'.", status=404,
                            content_type="text/plain")
    if not month or month not in months:
        month = max(months.keys())
    mo = months.get(month) or {}
    items = mo.get("items", {}) or {}
    try:
        from datetime import date as _date
        month_label = _date(int(month[:4]), int(month[5:7]), 1).strftime("%B %Y")
    except Exception:
        month_label = month
    # Day-by-day breakdown from the per-transaction ledger. The month table above can't
    # show WHEN anything sold. Customer names are appended only for the market's own
    # owner — this page is otherwise readable by anyone.
    extra = ""
    try:
        import html as _h
        is_owner = bool(_require_owner(request, mid))
        daily = [d for d in (db.get_csn_daily_sales(mid, 400) or [])
                 if str(d.get("day", "")).startswith(month)]
        if daily:
            mx = max(1.0, max(float(d.get("income") or 0) for d in daily))
            bars = "".join(
                '<div title="{d} · {i:,.0f}c · {u:,} pcs · {c} buyers" '
                'style="flex:1;min-width:5px;height:{h}%;background:var(--green);opacity:.85"></div>'.format(
                    d=_h.escape(str(d["day"])), i=float(d.get("income") or 0),
                    u=int(d.get("units") or 0), c=int(d.get("customers") or 0),
                    h=max(2, int(float(d.get("income") or 0) / mx * 100)))
                for d in sorted(daily, key=lambda x: x["day"]))
            rows = "".join(
                "<tr><td>{d}</td><td>{i:,.0f}</td><td>{u:,}</td><td>{c}</td></tr>".format(
                    d=_h.escape(str(d["day"])), i=float(d.get("income") or 0),
                    u=int(d.get("units") or 0), c=int(d.get("customers") or 0))
                for d in sorted(daily, key=lambda x: x["day"], reverse=True))
            cust = ""
            if is_owner:
                tops = db.get_csn_top_customers(mid, 400, 12) or []
                if tops:
                    cust = ("<h2 style='font-size:15px;margin:20px 0 6px'>Top customers "
                            "<span style='color:var(--muted);font-weight:400;font-size:12px'>"
                            "(owner only)</span></h2><table><thead><tr><th>Player</th>"
                            "<th>Spent</th><th>Units</th><th>Buys</th></tr></thead><tbody>"
                            + "".join("<tr><td>{a}</td><td>{s:,.0f}</td><td>{u:,}</td><td>{t}</td></tr>".format(
                                a=_h.escape(str(c.get("actor") or "?")), s=float(c.get("spent") or 0),
                                u=int(c.get("units") or 0), t=int(c.get("txns") or 0)) for c in tops)
                            + "</tbody></table>")
            extra = ("<h2 style='font-size:15px;margin:22px 0 6px'>Sales by day</h2>"
                     "<div style='display:flex;align-items:flex-end;gap:3px;height:90px;"
                     "margin-bottom:12px'>" + bars + "</div>"
                     "<table><thead><tr><th>Day</th><th>Income</th><th>Units</th>"
                     "<th>Buyers</th></tr></thead><tbody>" + rows + "</tbody></table>" + cust)
    except Exception as _ee:
        print(f"[report] daily block failed for {mid} {month}: {_ee}")

    try:
        html = m._render_full_report_html(
            f"Monthly Report — {mname}", mname, month_label,
            items, float(mo.get("income", 0) or 0), float(mo.get("spent", 0) or 0),
            nav_html=_TERMINAL_NAV, extra_html=extra)
    except Exception as e:
        return web.Response(text=f"Could not render report: {e}", status=500,
                            content_type="text/plain")
    return web.Response(text=html, content_type="text/html")


async def _handle_health(request):
    return web.Response(text="ok")


# ── shared terminal shell (nav) for the remade pages ─────────────────────────
_TERMINAL_NAV = r"""
<header class="tshell">
  <div class="brand"><span class="m">A</span>ABEXILAS <span class="faint" style="font-weight:600">EXCHANGE</span></div>
  <nav>
    <a href="/inventory" data-nav="inventory">Inventory</a>
    <a href="/ledger" data-nav="ledger">Ledger</a>
    <a href="/exchange" data-nav="exchange">Exchange</a>
    <a href="/orders" data-nav="orders">Orders</a>
    <a href="/teams" data-nav="teams">Teams</a>
    <a href="/mymarket" data-nav="mymarket">My Market</a>
  </nav>
  <div class="rt"><div class="bp"><b class="mono" id="hWho">—</b><br><span id="hWhoSub">not linked</span></div>
    <button id="hAnon" style="margin-left:10px;background:none;border:1px solid currentColor;color:inherit;font:inherit;padding:3px 9px;cursor:pointer;display:none" title="Show or hide your name on the public shareholder leaderboard"></button>
    <button id="hAuth" style="margin-left:8px;background:none;border:1px solid currentColor;color:inherit;font:inherit;padding:3px 9px;cursor:pointer;display:none"></button>
  </div>
</header>
<script>document.addEventListener('DOMContentLoaded',()=>{const p=location.pathname.replace('/','')||'inventory';
const a=document.querySelector('[data-nav="'+p+'"]');if(a)a.classList.add('on');
// Account linking lives HERE, in the shared nav, so it works on every page. It used to
// exist only on the old /classic dashboard — which is why the other pages told people to
// "link on the old dashboard". Deleting that page without this would have broken login.
async function doLink(){
 const code=(window.prompt('Run /website_login in Discord, then paste your code here:')||'').trim();
 if(!code)return;
 try{const r=await fetch('/api/link',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({code})});const j=await r.json();
  if(j&&j.ok)location.reload();else alert((j&&j.error)||'Login failed.');}
 catch(e){alert('Login failed.');}}
async function doLogout(){try{await fetch('/api/logout',{method:'POST'});}catch(e){}location.reload();}
// Leaderboard anonymity. This toggle ONLY existed on the retired /classic page, so after
// it was removed there was no way to change it — while the leaderboard still used it.
function wireAnon(me){
 const b=document.getElementById('hAnon');if(!b)return;
 let anon=!!me.anonymous;
 const paint=()=>{b.textContent=anon?'Hidden':'Visible';
  b.title=anon?'Your name is hidden on the shareholder leaderboard — click to show it'
              :'Your name is public on the shareholder leaderboard — click to hide it';};
 paint();b.style.display='';
 b.onclick=async()=>{const want=!anon;b.disabled=true;
  try{const r=await fetch('/api/anon',{method:'POST',
    headers:{'Content-Type':'application/json','X-CSRF-Token':(me&&me.csrf)||''},
    body:JSON.stringify({anonymous:want})});
   const j=await r.json();if(j&&j.ok){anon=!!j.anonymous;paint();}}catch(e){}
  b.disabled=false;};}
fetch('/api/me').then(r=>r.json()).then(me=>{
 const btn=document.getElementById('hAuth');
 if(me&&me.logged_in){
  document.getElementById('hWho').textContent=me.name||'linked';
  document.getElementById('hWhoSub').textContent='Discord linked';
  window.OWNERINFO=me;
  wireAnon(me);
  if(btn){btn.textContent='Log out';btn.style.display='';btn.onclick=doLogout;}
 }else if(btn){btn.textContent='Log in';btn.style.display='';btn.onclick=doLink;}
}).catch(()=>{const btn=document.getElementById('hAuth');
 if(btn){btn.textContent='Log in';btn.style.display='';btn.onclick=doLink;}});});</script>
"""

_TERMINAL_CSS = r"""
:root{--bg:#0b0f10;--panel:#11171a;--panel2:#161d20;--row:#121a1c;--hover:#1a2427;--sel:#1c2a30;
--seam:#070b0b;--line:#212b2e;--line2:#2b3739;--ink:#d9e0e0;--ink2:#f0f4f4;--muted:#7a8a8a;--faint:#4b5a5a;
--up:#1fa97a;--down:#e5484d;--accent:#3f8fcf;--amber:#cfa637;
--sans:"IBM Plex Sans",-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:13px;-webkit-font-smoothing:antialiased}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
.up{color:var(--up)}.down{color:var(--down)}.muted{color:var(--muted)}.faint{color:var(--faint)}
header.tshell{display:flex;align-items:center;gap:20px;height:44px;padding:0 16px;border-bottom:1px solid var(--line);background:var(--panel)}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:14px;letter-spacing:.4px}
.brand .m{width:22px;height:22px;background:var(--up);color:#04120c;display:grid;place-items:center;font-weight:700;font-size:13px}
header.tshell nav{display:flex;gap:2px;height:100%;margin-left:6px}
header.tshell nav a{display:flex;align-items:center;padding:0 13px;color:var(--muted);font-weight:600;font-size:13px;cursor:pointer;
border-bottom:2px solid transparent;text-decoration:none}
header.tshell nav a.on{color:var(--ink2);border-bottom-color:var(--accent)}
header.tshell nav a:hover{color:var(--ink)}
.rt{margin-left:auto;display:flex;align-items:center;gap:14px}
.rt .bp{text-align:right;line-height:1.15}.rt .bp b{font-size:13px}
.rt .bp span{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.panel{background:var(--panel);border:1px solid var(--line)}
.ph{height:30px;display:flex;align-items:center;justify-content:space-between;padding:0 10px;background:var(--panel2);border-bottom:1px solid var(--line)}
.ph .t{font-size:10px;letter-spacing:.7px;text-transform:uppercase;color:var(--muted);font-weight:600}
.content{max-width:1240px;margin:0 auto;width:100%}
@media(min-width:1300px){.content{border-left:1px solid var(--line);border-right:1px solid var(--line)}}
"""

# ── /inventory — terminal Inventory page (Pass 1) ────────────────────────────
_INVENTORY_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Inventory · Abexilas</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>__TERMINAL_CSS__
.wrap{display:grid;grid-template-columns:1fr;gap:1px;background:var(--seam);padding:0}
.bar{display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
.chip{border:1px solid var(--line2);background:var(--panel2);color:var(--muted);font-family:var(--mono);font-size:11px;
padding:5px 10px;cursor:pointer;white-space:nowrap}
.chip.on{color:var(--ink2);border-color:var(--accent);box-shadow:inset 0 -2px 0 var(--accent)}
.chip:hover{color:var(--ink)}
.search{margin-left:auto;background:var(--bg);border:1px solid var(--line2);color:var(--ink);font-family:var(--mono);
font-size:12px;padding:6px 10px;width:220px;outline:none}
.search:focus{border-color:var(--accent)}
.gen{border:1px solid var(--up);background:transparent;color:var(--up);font-family:var(--sans);font-weight:600;
font-size:11px;letter-spacing:.4px;text-transform:uppercase;padding:6px 12px;cursor:pointer}
.gen:hover{background:var(--up);color:#04120c}
.statrow{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--seam)}
.stat{background:var(--panel);padding:9px 12px;border-bottom:1px solid var(--line)}
.stat .k{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600}
.stat .v{font-family:var(--mono);font-size:16px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
table.inv{width:100%;border-collapse:collapse}
table.inv{table-layout:fixed}
table.inv th{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600;text-align:right;
padding:6px 12px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel2);cursor:pointer;user-select:none}
table.inv th:first-child{text-align:left}
table.inv th:nth-child(2){width:215px}
table.inv th:nth-child(3),table.inv th:nth-child(4){width:105px}
table.inv th:nth-child(5){width:100px}
table.inv td:first-child{overflow:hidden;text-overflow:ellipsis}
tr.zero td:first-child{color:var(--muted)}
table.inv td .mkt{color:var(--accent);font-size:10px;margin-left:8px;font-family:var(--mono);text-transform:uppercase;letter-spacing:.3px;opacity:.85}
tr.zero .pct{opacity:.5}
table.inv th.sorted{color:var(--ink)}
table.inv td{padding:0 12px;height:28px;border-bottom:1px solid var(--row);font-size:12px;white-space:nowrap}
table.inv td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
table.inv tr:hover td{background:var(--hover)}
.fillcell{display:flex;align-items:center;gap:8px;justify-content:flex-end}
.fillbar{width:110px;height:5px;background:var(--row);border:1px solid var(--line);position:relative}
.fillbar i{position:absolute;left:0;top:0;bottom:0;display:block}
.pct{font-family:var(--mono);font-size:11px;width:38px;text-align:right}
.empty{padding:40px;text-align:center;color:var(--faint);font-size:12px}
.msg{font-size:11px;color:var(--muted);font-family:var(--mono)}
.catbar{overflow-x:auto}
table.inv tr.grp td{background:var(--panel);color:var(--faint);font-family:var(--sans);font-size:10px;
letter-spacing:.6px;text-transform:uppercase;font-weight:700;height:26px;text-align:left;
border-bottom:1px solid var(--line2);border-top:1px solid var(--line)}
table.inv tr.grp:hover td{background:var(--panel)}
table.inv tr.grp td .gcount{color:var(--accent);margin-left:6px;font-family:var(--mono)}
</style></head><body>
__NAV__
<div class="content">
<div class="bar" id="chips"></div>
<div class="statrow" id="stats"></div>
<div class="bar catbar" id="catchips"></div>
<div class="bar">
  <button class="gen" id="gen" style="display:none">Generate restock orders → 80%</button>
  <span class="msg" id="genmsg"></span>
  <div class="chip" id="grpbtn" style="cursor:pointer;white-space:nowrap">Σ Avg price</div>
  <input class="search" id="q" placeholder="Search items…" autocomplete="off">
</div>
<div class="panel" style="border-top:0">
<table class="inv"><thead><tr>
<th data-k="item" style="text-align:left">Item</th><th data-k="pct" class="sorted">Fullness ↑</th>
<th data-k="stock">In stock</th><th data-k="capacity">Capacity</th><th data-k="price">Price ¢</th>
</tr></thead><tbody id="tb"></tbody></table>
<div class="empty" id="empty" style="display:none">No barrel scan yet — press the stock-scan key in-game and click your shops.</div>
</div>
</div>
<script>
const INV=__INVENTORY_JSON__;
const fmt=n=>Math.round(n||0).toLocaleString('en-US').replace(/,/g,' ');
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
const DATA=(INV&&INV.markets)||[];
if(DATA.length>1){const _all=DATA.reduce((a,m)=>a.concat((m.items||[]).map(x=>Object.assign({},x,{_mkt:m.name||m.market_id}))),[]);
 DATA.unshift({market_id:"__all__",name:"All Markets",items:_all,count:_all.length,low:_all.filter(x=>x.capacity>0&&x.pct<=20).length});}
const CATORDER=["Wood & Logs","Ores & Minerals","Enchanted Gear","Redstone","Concrete & Clay","Nether","End","Ice & Snow","Farm & Food","Dyes & Wool","Mob Drops","Glass & Light","Nature","Building","Other"];
let act=0,sortK='pct',dir=1,catAct='All',grp=false;
const col=p=>p<=20?'var(--down)':(p<60?'var(--amber)':'var(--up)');
const catsIn=items=>{const c={};items.forEach(x=>{const k=x.cat||'Other';c[k]=(c[k]||0)+1;});
 const present=CATORDER.filter(k=>c[k]);
 const extra=Object.keys(c).filter(k=>!CATORDER.includes(k)).sort();
 return {order:[...present,...extra],counts:c};};
function chips(){document.getElementById('chips').innerHTML=DATA.map((m,i)=>
 '<div class="chip'+(i===act?' on':'')+'" data-i="'+i+'">'+esc(m.name||m.market_id)+' · '+m.count+'</div>').join('');
 document.querySelectorAll('#chips .chip').forEach(c=>c.onclick=()=>{act=+c.dataset.i;catAct='All';chips();catchips();render();});}
function catchips(){
 const mk=DATA[act]||{};const items=mk.items||[];const {order,counts}=catsIn(items);
 const cats=['All',...order];
 document.getElementById('catchips').innerHTML=cats.map(c=>
  '<div class="chip'+(c===catAct?' on':'')+'" data-c="'+esc(c)+'">'+esc(c)+' · '+(c==='All'?items.length:counts[c])+'</div>').join('');
 document.querySelectorAll('#catchips .chip').forEach(el=>el.onclick=()=>{catAct=el.dataset.c;catchips();render();});}
function rowHTML(x){const p=Math.max(0,Math.min(100,x.pct||0));
 return '<tr'+(((x.stock||0)<=0)?' class="zero"':'')+'><td>'+esc(x.item)+(x._mkt?'<span class="mkt">'+esc(x._mkt)+'</span>':'')+'</td>'+
  '<td class="num"><div class="fillcell"><div class="fillbar"><i style="width:'+p+'%;background:'+col(p)+'"></i></div>'+
  '<span class="pct" style="color:'+col(p)+'">'+Math.round(p)+'%</span></div></td>'+
  '<td class="num">'+fmt(x.stock)+'</td><td class="num">'+fmt(x.capacity)+'</td>'+
  '<td class="num">'+(x.price>0?(x.price<1?x.price.toFixed(2):fmt(x.price)):'—')+'</td></tr>';}
function groupRows(rows){
 const g={};
 for(const x of rows){const k=x.item;
  const e=g[k]||(g[k]={item:k,cat:x.cat,stock:0,capacity:0,prices:[],mkts:new Set()});
  e.stock+=x.stock||0;e.capacity+=x.capacity||0;
  if(x.price>0)e.prices.push(x.price);
  if(x._mkt)e.mkts.add(x._mkt);}
 return Object.values(g).map(e=>{
  const ps=e.prices.filter(p=>p>0);let price=0;
  if(ps.length){const mx=Math.max.apply(null,ps);const keep=ps.filter(p=>p>=0.2*mx);const a=keep.length?keep:ps;
   price=a.reduce((s,v)=>s+v,0)/a.length;}
  const pct=e.capacity>0?100*e.stock/e.capacity:0;
  const nm=e.mkts.size?(e.mkts.size+(e.mkts.size===1?" mkt":" mkts")):"";
  return {item:e.item,cat:e.cat,stock:e.stock,capacity:e.capacity,pct:Math.round(pct*10)/10,
          price:Math.round(price*100)/100,_mkt:nm};});}
function render(){
 const mk=DATA[act]||{};const items=mk.items||[];
 const gen=document.getElementById('gen');
 const owns=window.OWNERINFO&&(window.OWNERINFO.owned||[]).some(o=>String(o.mid)===String(mk.market_id));
 gen.style.display=owns?'':'none';gen.dataset.mid=mk.market_id||'';
 const low=items.filter(x=>x.capacity>0&&x.pct<=20).length;
 const cap=items.reduce((s,x)=>s+(x.capacity||0),0),st=items.reduce((s,x)=>s+(x.stock||0),0);
 const avg=cap?Math.round(100*st/cap):0;
 document.getElementById('stats').innerHTML=[
  ['Items',items.length,''],['Low ≤20%',low,low>0?'style="color:var(--down)"':''],
  ['Avg fullness',avg+'%','style="color:'+col(avg)+'"'],['Shelf units',fmt(st),'']].map(s=>
  '<div class="stat"><div class="k">'+s[0]+'</div><div class="v" '+s[2]+'>'+s[1]+'</div></div>').join('');
 const q=(document.getElementById('q').value||'').toLowerCase();
 let rows=items.filter(x=>(x.item||'').toLowerCase().includes(q));
 if(catAct!=='All')rows=rows.filter(x=>(x.cat||'Other')===catAct);
 if(grp)rows=groupRows(rows);
 rows.sort((a,b)=>{let x=a[sortK],y=b[sortK];
  if(typeof x==='string')return x.localeCompare(y)*dir;return ((x||0)-(y||0))*dir;});
 document.getElementById('empty').style.display=(DATA.length&&items.length)?'none':'';
 const html=rows.map(rowHTML).join('');
 document.getElementById('tb').innerHTML=html
  ||'<tr><td colspan="5" class="faint" style="height:34px">No items match.</td></tr>';}
document.getElementById('q').oninput=render;
document.getElementById('grpbtn').onclick=()=>{grp=!grp;document.getElementById('grpbtn').classList.toggle('on',grp);render();};
document.querySelectorAll('th[data-k]').forEach(th=>th.onclick=()=>{
 const k=th.dataset.k;if(sortK===k)dir=-dir;else{sortK=k;dir=1;}
 document.querySelectorAll('th[data-k]').forEach(t=>{t.classList.toggle('sorted',t.dataset.k===sortK);
  t.textContent=t.textContent.replace(/ [↑↓]$/,'')+(t.dataset.k===sortK?(dir===1?' ↑':' ↓'):'');});render();});
document.getElementById('gen').onclick=async()=>{
 const mid=document.getElementById('gen').dataset.mid;const msg=document.getElementById('genmsg');
 msg.textContent='working…';
 try{const r=await fetch('/api/owner/generate_orders',{method:'POST',
  headers:{'Content-Type':'application/json','X-CSRF-Token':(window.OWNERINFO&&window.OWNERINFO.csrf)||''},
  body:JSON.stringify({market_id:mid})});
  const d=await r.json();msg.textContent=d.ok?('created '+(d.created??'?')+' order(s)'):(d.error||'failed');}
 catch(e){msg.textContent='failed';}};
window.addEventListener('load',()=>{setTimeout(()=>{chips();catchips();render();},60);});
setTimeout(()=>{chips();catchips();render();},400);
</script></body></html>"""


async def _handle_inventory_page(request):
    inventory = _cached("inventory", _load_inventory_data)
    html = (_INVENTORY_HTML
            .replace("__TERMINAL_CSS__", _TERMINAL_CSS)
            .replace("__NAV__", _TERMINAL_NAV)
            .replace("__INVENTORY_JSON__", _jscript(inventory)))
    return web.Response(text=html, content_type="text/html")


# ── /ledger — terminal Earnings page (Pass 2) ────────────────────────────────
_LEDGER_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Ledger · Abexilas</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>__TERMINAL_CSS__
.bar{display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
.chip{border:1px solid var(--line2);background:var(--panel2);color:var(--muted);font-family:var(--mono);font-size:11px;padding:5px 10px;cursor:pointer;white-space:nowrap}
.chip.on{color:var(--ink2);border-color:var(--accent);box-shadow:inset 0 -2px 0 var(--accent)}
.chip:hover{color:var(--ink)}
.statrow{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--seam)}
.stat{background:var(--panel);padding:9px 12px;border-bottom:1px solid var(--line)}
.stat .k{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600}
.stat .v{font-family:var(--mono);font-size:16px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
.chartwrap{position:relative;padding:8px 6px 6px;background:var(--panel);border-bottom:1px solid var(--line)}
svg.chart{width:100%;height:220px;display:block}
.tip{position:absolute;pointer-events:none;background:var(--panel2);border:1px solid var(--line2);padding:4px 8px;font-size:11px;font-family:var(--mono);transform:translate(-50%,-135%);white-space:nowrap;opacity:0}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--seam)}
@media(max-width:1000px){.cols{grid-template-columns:1fr}}
table.t{width:100%;border-collapse:collapse}
table.t th{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600;text-align:right;padding:6px 12px;border-bottom:1px solid var(--line);background:var(--panel2)}
table.t th:first-child{text-align:left}
table.t td{padding:0 12px;height:27px;border-bottom:1px solid var(--row);font-size:12px;white-space:nowrap}
table.t td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
table.t tr:hover td{background:var(--hover)}
table.t th.sort{cursor:pointer;user-select:none}
table.t th.sort:hover{color:var(--ink)}
table.t th .ar{font-size:8px;opacity:.7;margin-left:2px}
.mmbadge{font-family:var(--mono);font-size:10px;margin-left:6px;font-variant-numeric:tabular-nums}
.split{display:inline-flex;flex-direction:column;gap:2px;width:120px;vertical-align:middle}
.split i{height:4px;display:block;background:var(--row)}
.msel{background:var(--bg);border:1px solid var(--line2);color:var(--ink);font-family:var(--mono);font-size:11px;padding:3px 7px;outline:none;margin-left:auto}
.msel:focus{border-color:var(--accent)}
.ph.rowh{display:flex;align-items:center;gap:8px}
.tblscroll{overflow-x:auto}
a.molink{color:var(--ink);text-decoration:none;border-bottom:1px dotted var(--line2)}
a.molink:hover{color:var(--accent);border-bottom-color:var(--accent)}
</style></head><body>
__NAV__
<div class="content">
<div class="bar" id="chips"></div>
<div class="statrow" id="stats"></div>
<div class="bar" id="metric"></div>
<div class="chartwrap"><svg class="chart" id="chart" preserveAspectRatio="none"></svg><div class="tip" id="tip"></div></div>
<div class="panel"><div class="ph"><span class="t">Monthly ledger</span></div>
<div class="tblscroll"><table class="t"><thead><tr><th>Month</th><th>Income</th><th>Spent</th><th>Net</th><th>MoM</th><th>Income vs spent</th></tr></thead><tbody id="mt"></tbody></table></div></div>
<div class="panel"><div class="ph rowh"><span class="t">Top items</span><select class="msel" id="msel"></select></div>
<div class="tblscroll"><table class="t"><thead><tr id="ith"></tr></thead><tbody id="it"></tbody></table></div></div>
</div>
<script>
const EF=__EARNFULL_JSON__;
const fmt=n=>Math.round(n||0).toLocaleString('en-US').replace(/,/g,' ');
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const MS=(EF&&EF.markets)||[];let act=0;
let METRIC='net';                 // chart line: net | income | spent
let MSEL='__life';                // top-items scope: '__life' or a month key
let SORT={k:'net',dir:-1};        // top-items sort column + direction
const METRICS=[['net','Net'],['income','Income'],['spent','Spent']];
// item table columns: key, label, numeric-getter (from an aggregated row)
const ICOLS=[['item','Item',null],['s','Sold',r=>r.s],['b','Bought',r=>r.b],
 ['inc','Income',r=>r.inc],['exp','Expense',r=>r.exp],['net','Net ¢',r=>r.net],
 ['mgn','Margin %',r=>r.inc>0?r.net/r.inc*100:-1e18],['vel','Vel ×',r=>r.vel]];
function chips(){document.getElementById('chips').innerHTML=MS.map((m,i)=>
 '<div class="chip'+(i===act?' on':'')+'" data-i="'+i+'">'+esc(m.name||m.id)+' · '+(m.months||[]).length+' mo</div>').join('');
 document.querySelectorAll('#chips .chip').forEach(c=>c.onclick=()=>{act=+c.dataset.i;MSEL='__life';chips();render();});}
function metricChips(){document.getElementById('metric').innerHTML=METRICS.map(([k,l])=>
 '<div class="chip'+(k===METRIC?' on':'')+'" data-k="'+k+'">'+l+'</div>').join('');
 document.querySelectorAll('#metric .chip').forEach(c=>c.onclick=()=>{METRIC=c.dataset.k;render();});}
function pathD(vals,w,h,pad,mn,rg){
 return vals.map((v,i)=>{const x=pad+i/((vals.length-1)||1)*(w-2*pad);const y=pad+(1-(v-mn)/rg)*(h-2*pad);
 return (i?'L':'M')+x.toFixed(1)+' '+y.toFixed(1);}).join(' ');}
// margin cell colouring: profitable → up, thin/negative → down
function mgnCell(r){if(!(r.inc>0))return '<td class="num faint">—</td>';
 const p=r.net/r.inc*100;return '<td class="num" style="color:'+(p>=0?css('--up'):css('--down'))+'">'+p.toFixed(0)+'%</td>';}
function render(){const m=MS[act]||{};const mo=m.months||[];
 const nets=mo.map(x=>x.net||0);const life=nets.reduce((a,b)=>a+b,0);
 const best=mo.length?mo.reduce((a,b)=>(b.net>a.net?b:a)):null;
 const last=mo[mo.length-1];
 document.getElementById('stats').innerHTML=[
  ['Lifetime net',fmt(life)+' ¢','style="color:'+(life>=0?css('--up'):css('--down'))+'"'],
  ['Months tracked',mo.length,''],
  ['Best month',best?(best.label+' · '+fmt(best.net)):'—',''],
  ['Last month net',last?fmt(last.net)+' ¢':'—',last?('style="color:'+(last.net>=0?css('--up'):css('--down'))+'"'):'']]
  .map(s=>'<div class="stat"><div class="k">'+s[0]+'</div><div class="v" '+(s[2]||'')+'>'+s[1]+'</div></div>').join('');
 metricChips();
 // ── chart: income/spent bars (always) + selected-metric line ────────────────
 const el=document.getElementById('chart');const w=el.clientWidth||900,h=220,pad=14;
 el.setAttribute('viewBox','0 0 '+w+' '+h);
 const line=mo.map(x=>x[METRIC]||0);const v=line.length>1?line:[0,0];
 const col=METRIC==='spent'?css('--down'):(v[v.length-1]>=0?css('--up'):css('--down'));
 let grid='';for(let i=0;i<5;i++){const y=pad+i/4*(h-2*pad);
  grid+='<line x1="'+pad+'" y1="'+y+'" x2="'+(w-pad)+'" y2="'+y+'" stroke="'+css('--line')+'" stroke-width="1"/>';}
 const mn=Math.min(...v,0),mx=Math.max(...v,1),rg=(mx-mn)||1;
 const zy=pad+(1-(0-mn)/rg)*(h-2*pad);
 // faint income (up) vs spent (down) bars behind the line, scaled together
 const barMax=Math.max(1,...mo.map(x=>Math.max(x.income||0,x.spent||0)));
 const n=mo.length,bw=n?Math.max(2,(w-2*pad)/n*0.34):0;let bars='';
 mo.forEach((x,i)=>{const cx=pad+(n>1?i/(n-1):0.5)*(w-2*pad);
  const ih=(x.income||0)/barMax*(h-2*pad)*0.5,sh=(x.spent||0)/barMax*(h-2*pad)*0.5;
  bars+='<rect x="'+(cx-bw-1)+'" y="'+(zy-ih)+'" width="'+bw+'" height="'+ih+'" fill="'+css('--up')+'" opacity="0.16"/>'+
        '<rect x="'+(cx+1)+'" y="'+zy+'" width="'+bw+'" height="'+sh+'" fill="'+css('--down')+'" opacity="0.16"/>';});
 el.innerHTML=grid+bars+'<line x1="'+pad+'" y1="'+zy+'" x2="'+(w-pad)+'" y2="'+zy+'" stroke="'+css('--line2')+'" stroke-width="1" stroke-dasharray="3 3"/>'+
  '<path d="'+pathD(v,w,h,pad,mn,rg)+'" fill="none" stroke="'+col+'" stroke-width="1.4"/>'+
  '<circle id="dot" r="3" fill="'+col+'" style="opacity:0"/>';
 const tip=document.getElementById('tip'),dot=document.getElementById('dot');
 el.onmousemove=e=>{const r=el.getBoundingClientRect();let i2=Math.round((e.clientX-r.left)/r.width*(v.length-1));
  i2=Math.max(0,Math.min(v.length-1,i2));const x=pad+i2/((v.length-1)||1)*(w-2*pad),y=pad+(1-(v[i2]-mn)/rg)*(h-2*pad);
  dot.setAttribute('cx',x);dot.setAttribute('cy',y);dot.style.opacity=1;
  tip.style.left=(x/w*100)+'%';tip.style.top=(y/h*100)+'%';tip.style.opacity=1;
  tip.textContent=(mo[i2]?mo[i2].label+': ':'')+fmt(v[i2])+' ¢';};
 el.onmouseleave=()=>{dot.style.opacity=0;tip.style.opacity=0;};
 // ── month table (newest first) with MoM % + income/spent split bar ──────────
 const smax=Math.max(1,...mo.map(x=>Math.max(x.income||0,x.spent||0)));
 document.getElementById('mt').innerHTML=mo.map((x,i)=>{
   const prev=i>0?mo[i-1]:null;let mom='<span class="faint">—</span>';
   if(prev){const d=(prev.net||0)!==0?((x.net-prev.net)/Math.abs(prev.net)*100):(x.net?100:0);
     mom='<span class="mmbadge" style="color:'+(d>=0?css('--up'):css('--down'))+'">'+(d>=0?'+':'')+d.toFixed(0)+'%</span>';}
   const iw=(x.income||0)/smax*118,sw=(x.spent||0)/smax*118;
   const rep='/report/'+encodeURIComponent(m.id||'')+'/'+encodeURIComponent(x.month||'');
   return {i,html:'<tr><td><a class="molink" href="'+rep+'" title="Open full report">'+esc(x.label||x.month)+'</a></td><td class="num">'+fmt(x.income)+'</td>'+
    '<td class="num">'+fmt(x.spent)+'</td><td class="num" style="color:'+((x.net||0)>=0?css('--up'):css('--down'))+'">'+fmt(x.net)+'</td>'+
    '<td class="num">'+mom+'</td>'+
    '<td><span class="split"><i style="width:'+iw+'px;background:'+css('--up')+'"></i>'+
    '<i style="width:'+sw+'px;background:'+css('--down')+'"></i></span></td></tr>'};
  }).reverse().map(o=>o.html).join('')
  ||'<tr><td colspan="6" class="faint" style="height:34px">No earnings recorded.</td></tr>';
 // ── top items: month selector, drill-down, sortable, margin + velocity ──────
 const sel=document.getElementById('msel');
 sel.innerHTML='<option value="__life">Lifetime</option>'+mo.slice().reverse().map(x=>
   '<option value="'+esc(x.month)+'">'+esc(x.label||x.month)+'</option>').join('');
 sel.value=MSEL;sel.onchange=()=>{MSEL=sel.value;renderItems();};
 renderItems();}
function renderItems(){const m=MS[act]||{};const mo=m.months||[];
 const src=MSEL==='__life'?mo:mo.filter(x=>x.month===MSEL);
 const agg={};src.forEach(x=>(x.items||[]).forEach(it=>{const e=agg[it.item]=agg[it.item]||
   {s:0,b:0,net:0,inc:0,exp:0,vel:0};
   e.s+=it.sold||0;e.b+=it.bought||0;e.net+=it.net||0;
   e.inc+=it.income||0;e.exp+=it.expense||0;e.vel+=it.tsold||0;}));
 // header (sortable)
 document.getElementById('ith').innerHTML=ICOLS.map(([k,l,g])=>
   '<th class="sort" data-k="'+k+'">'+l+(SORT.k===k?'<span class="ar">'+(SORT.dir<0?'▼':'▲')+'</span>':'')+'</th>').join('');
 document.querySelectorAll('#ith .sort').forEach(th=>th.onclick=()=>{const k=th.dataset.k;
   if(SORT.k===k)SORT.dir*=-1;else{SORT.k=k;SORT.dir=(k==='item')?1:-1;}renderItems();});
 const getv=ICOLS.find(c=>c[0]===SORT.k)[2];
 let rows=Object.entries(agg);
 rows.sort((a,b)=>{if(SORT.k==='item')return SORT.dir*a[0].localeCompare(b[0]);
   return SORT.dir*((getv(a[1])||0)-(getv(b[1])||0));});
 rows=rows.slice(0,40);
 document.getElementById('it').innerHTML=rows.map(([k,e])=>
   '<tr><td>'+esc(k)+'</td><td class="num">'+fmt(e.s)+'</td><td class="num">'+fmt(e.b)+'</td>'+
   '<td class="num">'+fmt(e.inc)+'</td><td class="num" style="color:'+css('--down')+'">'+fmt(e.exp)+'</td>'+
   '<td class="num" style="color:'+(e.net>=0?css('--up'):css('--down'))+'">'+fmt(e.net)+'</td>'+
   mgnCell(e)+'<td class="num muted">'+fmt(e.vel)+'</td></tr>').join('')
  ||'<tr><td colspan="8" class="faint" style="height:34px">No item data.</td></tr>';}
chips();render();addEventListener('resize',render);
</script></body></html>"""


async def _handle_ledger_page(request):
    ef = _cached("earnings_full", _load_earnings_full)
    html = (_LEDGER_HTML.replace("__TERMINAL_CSS__", _TERMINAL_CSS)
            .replace("__NAV__", _TERMINAL_NAV).replace("__EARNFULL_JSON__", _jscript(ef)))
    return web.Response(text=html, content_type="text/html")


# ── /orders — terminal Orders board (Pass 3) ─────────────────────────────────
_ORDERS_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Orders · Abexilas</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>__TERMINAL_CSS__
.bar{display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
.chip{border:1px solid var(--line2);background:var(--panel2);color:var(--muted);font-family:var(--mono);font-size:11px;padding:5px 10px;cursor:pointer;white-space:nowrap}
.chip.on{color:var(--ink2);border-color:var(--accent);box-shadow:inset 0 -2px 0 var(--accent)}
.chip:hover{color:var(--ink)}
.statrow{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--seam)}
.stat{background:var(--panel);padding:9px 12px;border-bottom:1px solid var(--line)}
.stat .k{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600}
.stat .v{font-family:var(--mono);font-size:16px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
table.t{width:100%;border-collapse:collapse}
table.t th{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600;text-align:right;padding:6px 12px;border-bottom:1px solid var(--line);background:var(--panel2)}
table.t th:nth-child(1),table.t th:nth-child(2){text-align:left}
table.t td{padding:0 12px;height:28px;border-bottom:1px solid var(--row);font-size:12px;white-space:nowrap}
table.t td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
table.t tr:hover td{background:var(--hover)}
.tag{display:inline-block;font-size:9.5px;font-weight:600;letter-spacing:.4px;padding:1px 6px;border:1px solid;border-radius:2px;font-family:var(--mono);text-transform:uppercase}
.pbar{width:120px;height:5px;background:var(--row);border:1px solid var(--line);position:relative;display:inline-block;vertical-align:middle}
.pbar i{position:absolute;left:0;top:0;bottom:0}
.place{padding:10px 12px;background:var(--panel);border-bottom:1px solid var(--line)}
.place .row{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap}
.fld label{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600;display:block;margin-bottom:4px}
.fld input{background:var(--bg);border:1px solid var(--line2);color:var(--ink);font-family:var(--mono);font-size:12px;padding:6px 9px;outline:none}
.fld input:focus{border-color:var(--accent)}
.btn{border:1px solid var(--accent);background:transparent;color:var(--accent);font-weight:600;font-size:11px;
letter-spacing:.4px;text-transform:uppercase;padding:6px 12px;cursor:pointer;font-family:var(--sans)}
.btn:hover{background:var(--accent);color:#04120c}
.btn.go{border-color:var(--up);color:var(--up)}.btn.go:hover{background:var(--up)}
.msg{font-size:11px;color:var(--muted);font-family:var(--mono)}
table.cart{border-collapse:collapse;margin-top:8px}
table.cart td{padding:3px 10px 3px 0;font-size:12px}
.x{color:var(--down);cursor:pointer;font-family:var(--mono)}
</style></head><body>
__NAV__
<div class="content">
<div class="place">
  <div id="locked" class="msg">Log in to order — run <span class="mono">/website_login</span> in Discord, then hit <b>Log in</b> at the top right.</div>
  <div id="form" style="display:none">
    <div class="row">
      <div class="fld"><label>Item</label><input id="oi" list="cat" style="width:230px" placeholder="Search catalog…"><datalist id="cat"></datalist></div>
      <div class="fld"><label>Qty</label><input id="oq" type="number" min="1" value="64" style="width:80px"></div>
      <button class="btn" id="add">Add</button>
      <div class="fld" style="flex:1;min-width:180px"><label>Notes</label><input id="on" style="width:100%" placeholder="optional — e.g. deliver to spawn"></div>
      <button class="btn go" id="sub">Submit order</button><span class="msg" id="m"></span>
    </div>
    <table class="cart" id="cart"></table>
  </div>
</div>
<div class="bar" id="chips"></div>
<div class="statrow" id="stats"></div>
<div class="panel" style="border-top:0">
<table class="t"><thead><tr><th style="width:52px">#</th><th>Item</th><th>Requested</th><th>Claimed</th><th>Progress</th><th style="width:110px">Status</th></tr></thead>
<tbody id="tb"></tbody></table>
<div id="empty" class="faint" style="display:none;padding:36px;text-align:center;font-size:12px">No open orders — all caught up.</div>
</div>
</div>
<script>
const OD=__ORDERS_JSON__;const ITEMS=__ITEMS_JSON__;
const fmt=n=>Math.round(n||0).toLocaleString('en-US').replace(/,/g,' ');
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const MS=(OD&&OD.markets)||[];let act=0,cart=[];
const STC={open:'--accent',claimed:'--amber',partial:'--amber',in_progress:'--amber',ready:'--up',pending:'--muted'};
function chips(){document.getElementById('chips').innerHTML=MS.map((m,i)=>
 '<div class="chip'+(i===act?' on':'')+'" data-i="'+i+'">'+esc(m.name||m.market_id)+' · '+m.count+'</div>').join('');
 document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{act=+c.dataset.i;chips();render();});}
function render(){const mk=MS[act]||{};const os=mk.orders||[];
 const open=os.length,units=os.reduce((a,o)=>a+(o.requested||0),0),
 done=os.reduce((a,o)=>a+Math.min(o.claimed||0,o.requested||0),0);
 document.getElementById('stats').innerHTML=[['Open orders',open],['Units requested',fmt(units)],
  ['Units claimed',fmt(done)],['Fill rate',(units?Math.round(100*done/units):0)+'%']].map(s=>
  '<div class="stat"><div class="k">'+s[0]+'</div><div class="v">'+s[1]+'</div></div>').join('');
 document.getElementById('empty').style.display=os.length?'none':'';
 document.getElementById('tb').innerHTML=os.map(o=>{
  const p=o.requested?Math.min(100,100*(o.claimed||0)/o.requested):0;
  const st=String(o.status||'open').toLowerCase();const c=css(STC[st]||'--muted');
  return '<tr><td class="num muted">'+o.id+'</td><td>'+esc(o.item)+'</td>'+
  '<td class="num">'+fmt(o.requested)+'</td><td class="num muted">'+fmt(o.claimed)+'</td>'+
  '<td class="num"><span class="pbar"><i style="width:'+p+'%;background:'+(p>=100?css('--up'):css('--amber'))+'"></i></span> '+
  '<span class="mono" style="font-size:11px">'+Math.round(p)+'%</span></td>'+
  '<td class="num"><span class="tag" style="color:'+c+';border-color:'+c+';background:'+c+'1c">'+esc(st)+'</span></td></tr>';}).join('');}
function cartR(){const t=document.getElementById('cart');
 t.innerHTML=cart.map((c,i)=>{const px=(ITEMS[c.item]&&ITEMS[c.item].coin)||0;
  return '<tr><td>'+esc(c.item)+'</td><td class="mono">×'+c.qty+'</td>'+
  '<td class="mono muted">'+(px?('≈ '+fmt(px*c.qty)+' ¢'):'')+'</td>'+
  '<td class="x" data-i="'+i+'">✕</td></tr>';}).join('');
 t.querySelectorAll('.x').forEach(x=>x.onclick=()=>{cart.splice(+x.dataset.i,1);cartR();});}
document.getElementById('add').onclick=()=>{const it=document.getElementById('oi').value.trim();
 const q=+document.getElementById('oq').value||0;if(!it||q<=0)return;
 cart.push({item:it,qty:q});document.getElementById('oi').value='';cartR();};
document.getElementById('sub').onclick=async()=>{const m=document.getElementById('m');
 if(!cart.length){m.textContent='cart is empty';return;}
 m.textContent='submitting…';
 try{const r=await fetch('/api/order',{method:'POST',headers:{'Content-Type':'application/json',
  'X-CSRF-Token':(window.OWNERINFO&&window.OWNERINFO.csrf)||''},
  body:JSON.stringify({items:cart,notes:document.getElementById('on').value})});
  const d=await r.json();m.textContent=d.ok?'order placed ✓':(d.error||'failed');
  if(d.ok){cart=[];cartR();}}catch(e){m.textContent='failed';}};
window.addEventListener('load',()=>{setTimeout(()=>{
 if(window.OWNERINFO&&window.OWNERINFO.logged_in){
  document.getElementById('locked').style.display='none';
  document.getElementById('form').style.display='';
  document.getElementById('cat').innerHTML=Object.keys(ITEMS||{}).sort().map(k=>'<option value="'+esc(k)+'">').join('');}
 },350);});
chips();render();
</script></body></html>"""


async def _handle_orders_page(request):
    orders = _cached("orders_board", _load_orders_data)
    items = _cached("items", _load_items)
    html = (_ORDERS_HTML.replace("__TERMINAL_CSS__", _TERMINAL_CSS)
            .replace("__NAV__", _TERMINAL_NAV)
            .replace("__ORDERS_JSON__", _jscript(orders))
            .replace("__ITEMS_JSON__", _jscript(items)))
    return web.Response(text=html, content_type="text/html")


# ── /teams — terminal Teams leaderboard (Pass 4a) ────────────────────────────
_TEAMS_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Teams · Abexilas</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>__TERMINAL_CSS__
table.t{width:100%;border-collapse:collapse}
table.t th{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600;text-align:right;padding:6px 12px;border-bottom:1px solid var(--line);background:var(--panel2)}
table.t th:nth-child(1),table.t th:nth-child(2){text-align:left}
table.t td{padding:0 12px;height:30px;border-bottom:1px solid var(--row);font-size:12px;white-space:nowrap}
table.t td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
table.t tr:hover td{background:var(--hover)}
.sub{font-size:10px;color:var(--faint);padding:8px 12px}
.wk{font-size:10.5px;color:var(--muted)}
</style></head><body>
__NAV__
<div class="content">
<div class="panel" style="border-top:0">
<div class="ph"><span class="t">Team leaderboard · last <span id="d">7</span> days</span><span class="t mono" id="gen"></span></div>
<table class="t"><thead><tr><th style="width:40px">#</th><th>Team</th><th>Members</th><th>Orders</th><th>Order ¢</th><th>Sales ¢</th><th>Futures</th><th>Total ¢</th></tr></thead>
<tbody id="tb"></tbody></table>
<div id="empty" class="faint" style="display:none;padding:36px;text-align:center;font-size:12px">No team activity yet.</div>
<div class="sub">Ranked by total coins (order payouts + chest-shop sales). In-game names only — no Discord IDs.</div>
</div>
</div>
<script>
const TD=__TEAMS_JSON__;
const fmt=n=>Math.round(n||0).toLocaleString('en-US').replace(/,/g,' ');
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
const ts=(TD&&TD.teams)||[];
document.getElementById('d').textContent=(TD&&TD.days)||7;
document.getElementById('gen').textContent=(TD&&TD.generated)||'';
document.getElementById('empty').style.display=ts.length?'none':'';
document.getElementById('tb').innerHTML=ts.map((t,i)=>{
 const wk=(t.top_workers||[]).map(w=>esc(w.ign)+' '+fmt(w.coins)).join(' · ');
 return '<tr><td class="num muted">'+(i+1)+'</td>'+
 '<td>'+esc(t.captain)+(wk?('<div class="wk">'+wk+'</div>'):'')+'</td>'+
 '<td class="num">'+t.members+'</td><td class="num">'+t.orders+'</td>'+
 '<td class="num">'+fmt(t.order_coins)+'</td><td class="num">'+fmt(t.sales_coins)+'</td>'+
 '<td class="num">'+fmt(t.futures)+'</td><td class="num" style="font-weight:600">'+fmt(t.total)+'</td></tr>';}).join('');
</script></body></html>"""


async def _handle_teams_page(request):
    teams = _cached("teams_data", _load_teams_data)
    html = (_TEAMS_HTML.replace("__TERMINAL_CSS__", _TERMINAL_CSS)
            .replace("__NAV__", _TERMINAL_NAV).replace("__TEAMS_JSON__", _jscript(teams)))
    return web.Response(text=html, content_type="text/html")


# ── /mymarket — terminal owner panel (Pass 4b) ───────────────────────────────
_MYMARKET_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>My Market · Abexilas</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>__TERMINAL_CSS__
.bar{display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
.chip{border:1px solid var(--line2);background:var(--panel2);color:var(--muted);font-family:var(--mono);font-size:11px;padding:5px 10px;cursor:pointer;white-space:nowrap}
.chip.on{color:var(--ink2);border-color:var(--accent);box-shadow:inset 0 -2px 0 var(--accent)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--seam)}
@media(max-width:1000px){.cols{grid-template-columns:1fr}}
.pb{padding:10px 12px}
.row{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap}
.fld label{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600;display:block;margin-bottom:4px}
.fld input{background:var(--bg);border:1px solid var(--line2);color:var(--ink);font-family:var(--mono);font-size:12px;padding:6px 9px;outline:none}
.fld input:focus{border-color:var(--accent)}
.btn{border:1px solid var(--accent);background:transparent;color:var(--accent);font-weight:600;font-size:11px;letter-spacing:.4px;text-transform:uppercase;padding:6px 12px;cursor:pointer;font-family:var(--sans)}
.btn:hover{background:var(--accent);color:#04120c}
.btn.up{border-color:var(--up);color:var(--up)}.btn.up:hover{background:var(--up)}
.btn.danger{border-color:var(--down);color:var(--down)}.btn.danger:hover{background:var(--down);color:#fff}
.msg{font-size:11px;color:var(--muted);font-family:var(--mono)}
.note{font-size:10px;color:var(--faint);margin-top:6px}
.locked{padding:60px;text-align:center;color:var(--faint)}
.statrow{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--seam)}
@media(max-width:1000px){.statrow{grid-template-columns:repeat(2,1fr)}}
.stat{background:var(--panel);padding:9px 12px;border-bottom:1px solid var(--line)}
.stat .k{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600}
.stat .v{font-family:var(--mono);font-size:16px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
.ph.rowh{justify-content:flex-start;gap:8px}
.msel{background:var(--bg);border:1px solid var(--line2);color:var(--ink);font-family:var(--mono);font-size:11px;padding:3px 7px;outline:none}
.msel:focus{border-color:var(--accent)}
table.t{width:100%;border-collapse:collapse}
table.t th{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600;text-align:right;padding:6px 12px;border-bottom:1px solid var(--line);background:var(--panel2)}
table.t th:first-child{text-align:left}
table.t th.sort{cursor:pointer;user-select:none}table.t th.sort:hover{color:var(--ink)}
table.t th .ar{font-size:8px;opacity:.7;margin-left:2px}
table.t td{padding:0 12px;height:27px;border-bottom:1px solid var(--row);font-size:12px;white-space:nowrap}
table.t td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
table.t tr:hover td{background:var(--hover)}
.tblscroll{overflow-x:auto}
.rbox{max-height:56vh;overflow-y:auto}
.rbox table.t thead th{position:sticky;top:0;z-index:2}
.rrow{display:flex;gap:8px;align-items:center;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line)}
.rsearch{flex:1;min-width:160px;background:var(--bg);border:1px solid var(--line2);color:var(--ink);font-family:var(--mono);font-size:12px;padding:6px 9px;outline:none}
.rsearch:focus{border-color:var(--accent)}
.fill{display:inline-block;width:70px;height:5px;background:var(--row);border:1px solid var(--line);position:relative;vertical-align:middle;margin-right:6px}
.fill i{position:absolute;left:0;top:0;bottom:0}
a.btn{text-decoration:none;display:inline-block}
a.pick{color:var(--ink);text-decoration:none;border-bottom:1px dotted var(--line2);cursor:pointer}
a.pick:hover{color:var(--accent);border-bottom-color:var(--accent)}
@keyframes fl{0%{border-color:var(--accent);background:var(--sel)}100%{border-color:var(--line2);background:var(--bg)}}
.flash{animation:fl 1.2s ease}
.panel>.ph{cursor:pointer;user-select:none}
.ph .cx{display:inline-block;transition:transform .12s;color:var(--muted);margin-right:6px;font-size:10px}
.panel.collapsed>.ph .cx{transform:rotate(-90deg)}
.panel.collapsed>:not(.ph){display:none!important}
</style></head><body>
__NAV__
<div class="content">
<div id="locked" class="locked">Owner tools — run <span class="mono" style="color:var(--ink)">/website_login</span> in Discord, link on the dashboard, then reload.</div>
<div id="panel" style="display:none">
<div class="bar" id="chips"></div>
<div class="panel" style="border-top:0">
  <div class="ph rowh"><span class="t">Monthly report</span>
    <select class="msel" id="rmonth"></select>
    <span class="msg" id="rhint" style="margin-left:auto"></span>
  </div>
  <div class="statrow" id="rstats"></div>
  <div class="statrow" id="fstats"></div>
  <div class="rrow">
    <input class="rsearch" id="rq" placeholder="Search items…" autocomplete="off">
    <select class="msel" id="rf">
      <option value="all">All items</option>
      <option value="income">Income (net &gt; 0)</option>
      <option value="expense">Expense (net &lt; 0)</option>
    </select>
  </div>
  <div class="tblscroll rbox"><table class="t"><thead><tr id="rth"></tr></thead><tbody id="rtb"></tbody></table></div>
</div>
<div class="panel"><div class="ph rowh"><span class="t">Restock next</span><span class="msg" id="lsnote" style="margin-left:auto"></span></div>
  <div class="tblscroll"><table class="t"><thead><tr><th>Item</th><th>Fullness</th><th>In stock</th><th>Capacity</th><th>Need</th></tr></thead><tbody id="lstb"></tbody></table></div>
</div>
<div class="panel"><div class="ph rowh"><span class="t">Sales by day</span><span class="msg" id="dsnote" style="margin-left:auto"></span></div>
  <div class="pb">
    <div id="dsempty" class="msg" style="display:none">No per-transaction data yet — needs the CSN mod build that uploads the <span class="mono">csn_export</span> file next to the monthly report. Re-scan with <b>K</b> once you have it.</div>
    <div id="dswrap" style="display:none">
      <div class="statrow" id="dsstats"></div>
      <div id="dsbars" style="display:flex;align-items:flex-end;gap:3px;height:96px;margin:10px 0 14px"></div>
      <div class="cols">
        <div><div class="msg" style="margin-bottom:5px">Sold on <span class="mono" id="dsday"></span></div>
          <div class="tblscroll"><table class="t"><thead><tr><th>Item</th><th>Units</th><th>Coins</th></tr></thead><tbody id="dsitems"></tbody></table></div></div>
        <div><div class="msg" style="margin-bottom:5px">Top customers (30d)</div>
          <div class="tblscroll"><table class="t"><thead><tr><th>Player</th><th>Spent</th><th>Buys</th></tr></thead><tbody id="dscust"></tbody></table></div></div>
      </div>
    </div>
  </div>
</div>
<div class="cols">
  <div class="panel"><div class="ph"><span class="t">Restock rewards</span></div><div class="pb">
    <div class="row">
      <div class="fld"><label>Loyalty × points</label><input id="lm" type="number" step="0.1" min="0.1" style="width:90px"></div>
      <div class="fld"><label>Coin bonus / order</label><input id="lb" type="number" min="0" style="width:110px"></div>
      <div class="fld"><label>% bonus / order</label><input id="lp" type="number" min="0" step="1" style="width:90px"></div>
      <button class="btn" id="ls">Save</button><span class="msg" id="lmsg"></span>
    </div>
    <div class="note">Extra pay for workers who fill this market's orders. Synced with /market loyalty in Discord.</div>
  </div></div>
  <div class="panel"><div class="ph"><span class="t">Earnings privacy</span></div><div class="pb">
    <div class="row" style="align-items:center">
      <label style="display:flex;gap:8px;align-items:center;cursor:pointer">
        <input type="checkbox" id="pubchk"> <span>Show this market's earnings on the public Ledger</span>
      </label>
      <span class="msg" id="pubmsg"></span>
    </div>
    <div class="msg" style="margin-top:6px">Off = your income, spend and margins are hidden from the Ledger and your <span class="mono">/report</span> pages are owner-only. You always see your own figures here.</div>
  </div></div>
  <div class="panel"><div class="ph"><span class="t">Actions</span></div><div class="pb">
    <div class="row">
      <button class="btn up" id="gen">Generate restock orders → 80%</button>
      <span class="msg" id="gmsg"></span>
    </div>
    <div class="note">Creates worker orders from the real shortfall (capacity − stock). Same as the Inventory page button.</div>
  </div></div>
</div>
<div class="cols">
  <div class="panel"><div class="ph"><span class="t">Set item price / stock</span></div><div class="pb">
    <div class="row">
      <div class="fld"><label>Item</label><input id="si" list="cat" style="width:200px" placeholder="item name"><datalist id="cat"></datalist></div>
      <div class="fld"><label>Price ¢/unit</label><input id="sp" type="number" min="0" step="0.01" style="width:100px"></div>
      <div class="fld"><label>Stock</label><input id="ss" type="number" min="0" style="width:90px" placeholder="optional"></div>
      <button class="btn" id="sset">Set</button><span class="msg" id="smsg"></span>
    </div>
  </div></div>
  <div class="panel"><div class="ph"><span class="t">Log a restock</span></div><div class="pb">
    <div class="row">
      <div class="fld"><label>Item</label><input id="ri" list="cat" style="width:200px" placeholder="item name"></div>
      <div class="fld"><label>Qty added</label><input id="rqty" type="number" min="1" style="width:90px"></div>
      <div class="fld"><label>Cost ¢ (total)</label><input id="rc" type="number" min="0" style="width:110px"></div>
      <button class="btn" id="rlog">Log</button><span class="msg" id="rmsg"></span>
    </div>
    <div class="note">Records what you refilled by hand so margins stay honest.</div>
  </div></div>
</div>
<div class="panel"><div class="ph"><span class="t">Remove an item</span></div><div class="pb">
  <div class="row">
    <div class="fld"><label>Item</label><input id="di" list="cat" style="width:200px" placeholder="item name"></div>
    <button class="btn danger" id="del">Remove (full)</button><span class="msg" id="dmsg"></span>
  </div>
  <div class="note">Deletes from catalog, live shop list and earnings totals — the dashboard reflects it immediately.</div>
</div></div>
</div>
</div>
<script>
const fmt=n=>Math.round(n||0).toLocaleString('en-US').replace(/,/g,' ');
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
let owned=[],act=0;
const mid=()=> (owned[act]&&owned[act].mid)||'';
const csrf=()=> (window.OWNERINFO&&window.OWNERINFO.csrf)||'';
async function post(url,body){const r=await fetch(url,{method:'POST',
 headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify(body)});
 return r.json();}
function chips(){document.getElementById('chips').innerHTML=owned.map((m,i)=>
 '<div class="chip'+(i===act?' on':'')+'" data-i="'+i+'">'+esc(m.name||m.mid)+'</div>').join('');
 document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{act=+c.dataset.i;chips();loadMk();});}
async function loadMk(){
 try{const r=await fetch('/api/owner/loyalty?market_id='+encodeURIComponent(mid()));const d=await r.json();
  if(d&&d.ok!==false){document.getElementById('lm').value=d.pts_mult??d.mult??1;
   document.getElementById('lb').value=d.coin_bonus??0;document.getElementById('lp').value=d.pct_bonus??0;}}catch(e){}
 try{const r=await fetch('/api/owner/catalog?market_id='+encodeURIComponent(mid()));const d=await r.json();
  const names=[];(d&&d.groups?Object.values(d.groups):[]).forEach(g=>(g||[]).forEach(x=>names.push(x.item||x.name)));
  if(!names.length&&d&&Array.isArray(d.items))d.items.forEach(x=>names.push(x.item||x.name));
  document.getElementById('cat').innerHTML=names.sort().map(n=>'<option value="'+esc(n)+'">').join('');}catch(e){}
 loadReport();loadDaily();loadPrivacy();}

// ── Earnings privacy (owner-controlled Ledger visibility) ────────────────────
async function loadPrivacy(){
 const c=document.getElementById('pubchk');if(!c)return;
 try{const r=await fetch('/api/owner/privacy?market_id='+encodeURIComponent(mid()));
  const d=await r.json();if(d&&d.ok)c.checked=!!d.public;}catch(e){}
 c.onchange=async()=>{const m=document.getElementById('pubmsg');m.textContent='saving…';
  const d=await post('/api/owner/privacy',{market_id:mid(),public:c.checked});
  m.textContent=(d&&d.ok)?(c.checked?'public':'hidden'):'failed';
  setTimeout(()=>{m.textContent='';},2500);};}

// ── Sales by day (per-transaction ledger) ────────────────────────────────────
// The monthly report can only say what a MONTH totalled. csn_transactions keeps every
// individual sale with its timestamp and buyer, so this is the only view that can answer
// "what sold on Tuesday". Hidden entirely when a market has no transaction rows yet.
async function loadDaily(day){
 const wrap=document.getElementById('dswrap'),empty=document.getElementById('dsempty');
 if(!wrap)return;
 let d=null;
 try{const r=await fetch('/api/owner/sales?market_id='+encodeURIComponent(mid())+'&days=30'
      +(day?'&day='+encodeURIComponent(day):''));d=await r.json();}catch(e){}
 if(!d||!d.ok||!d.has_data){wrap.style.display='none';empty.style.display='';return;}
 empty.style.display='none';wrap.style.display='';
 const days=(d.daily||[]).slice().reverse();
 const inc=days.reduce((s,x)=>s+(x.income||0),0),un=days.reduce((s,x)=>s+(x.units||0),0);
 const best=days.reduce((b,x)=>(!b||(x.income||0)>(b.income||0))?x:b,null);
 document.getElementById('dsstats').innerHTML=
   [[fmt(inc),'Coins 30d'],[fmt(un),'Units'],[days.length,'Active days'],
    [best?best.day.slice(5):'—','Best day']]
   .map(([v,l])=>'<div class="stat"><div class="v">'+v+'</div><div class="l">'+l+'</div></div>').join('');
 const sel=day||(days.length?days[days.length-1].day:'');
 const mx=Math.max(1,...days.map(x=>x.income||0));
 const bars=document.getElementById('dsbars');bars.innerHTML='';
 days.forEach(x=>{const b=document.createElement('div');
  b.style.cssText='flex:1;min-width:5px;cursor:pointer;height:'+Math.max(2,Math.round((x.income||0)/mx*100))
   +'%;background:'+(x.day===sel?'var(--up)':'var(--line)');
  b.title=x.day+' · '+fmt(x.income)+'c · '+fmt(x.units)+' pcs · '+(x.customers||0)+' buyers';
  b.onclick=()=>loadDaily(x.day);bars.appendChild(b);});
 document.getElementById('dsday').textContent=sel||'—';
 document.getElementById('dsitems').innerHTML=(d.day_items||[]).map(x=>
   '<tr><td>'+esc(x.item)+'</td><td>'+fmt(x.units)+'</td><td>'+fmt(x.coins)+'</td></tr>').join('')
   ||'<tr><td colspan="3" class="msg">Click a bar to see that day.</td></tr>';
 document.getElementById('dscust').innerHTML=(d.top_customers||[]).map(x=>
   '<tr><td>'+esc(x.actor)+'</td><td>'+fmt(x.spent)+'</td><td>'+(x.txns||0)+'</td></tr>').join('')
   ||'<tr><td colspan="3" class="msg">No purchases in 30 days.</td></tr>';
 document.getElementById('dsnote').textContent=days.length?(fmt(inc)+'c over '+days.length+' day(s)'):'';
}

// ── Monthly report + fullness (per owned market) ─────────────────────────────
let EARN=null,RSORT={k:'net',dir:-1};
const RCOLS=[['item','Item',null],['sold','Sold',r=>r.sold],['bought','Bought',r=>r.bought],
 ['income','Income',r=>r.income],['expense','Expense',r=>r.expense],['net','Net ¢',r=>r.net],
 ['mgn','Margin %',r=>r.income>0?r.net/r.income*100:-1e18],['vel','Vel ×',r=>r.tsold]];
const fcol=p=>p<=20?'var(--down)':(p<60?'var(--amber)':'var(--up)');
function curMarket(){return ((EARN&&EARN.markets)||[]).find(x=>String(x.id)===String(mid()));}
function curMonth(){const mk=curMarket();const ms=(mk&&mk.months)||[];
 const v=document.getElementById('rmonth').value;return ms.find(x=>x.month===v)||ms[ms.length-1]||null;}
async function loadReport(){
 if(!EARN){try{EARN=await (await fetch('/api/earnings_full')).json();}catch(e){EARN={markets:[]};}}
 const mk=curMarket();const ms=(mk&&mk.months)||[];const sel=document.getElementById('rmonth');
 if(!ms.length){sel.innerHTML='<option>—</option>';
  document.getElementById('rstats').innerHTML='<div class="stat"><div class="k">No report data yet</div><div class="v faint">—</div></div>';
  document.getElementById('fstats').innerHTML='';document.getElementById('rth').innerHTML='';document.getElementById('rtb').innerHTML='';return;}
 sel.innerHTML=ms.slice().reverse().map(x=>'<option value="'+esc(x.month)+'">'+esc(x.label||x.month)+'</option>').join('');
 renderReport();
 try{const inv=await (await fetch('/api/owner/inventory?market_id='+encodeURIComponent(mid()))).json();
  const its=(inv&&inv.items)||[];renderFullness(its);renderLowStock(its);}
 catch(e){document.getElementById('fstats').innerHTML='';document.getElementById('lstb').innerHTML='';}}
function renderReport(){
 const mo=curMonth();if(!mo)return;
 const inc=mo.income||0,sp=mo.spent||0,net=mo.net||0,its=mo.items||[];
 document.getElementById('rstats').innerHTML=[
  ['Income',fmt(inc)+' ¢','style="color:var(--up)"'],
  ['Spent',fmt(sp)+' ¢','style="color:var(--down)"'],
  ['Net profit',(net>=0?'+':'')+fmt(net)+' ¢','style="color:'+(net>=0?'var(--up)':'var(--down)')+'"'],
  ['Items',its.length,''],
  ['Income SKUs',its.filter(x=>(x.net||0)>0).length,'']
 ].map(s=>'<div class="stat"><div class="k">'+s[0]+'</div><div class="v" '+(s[2]||'')+'>'+s[1]+'</div></div>').join('');
 // Months imported before per-item income/expense/velocity existed carry 0 there —
 // drop those columns (+ hint) rather than show a wall of zeros; they light up on re-scan.
 const detail=its.some(x=>(x.income||0)||(x.expense||0)||(x.tsold||0)||(x.tbought||0));
 const cols=detail?RCOLS:RCOLS.filter(c=>['item','sold','bought','net'].indexOf(c[0])>=0);
 if(!cols.some(c=>c[0]===RSORT.k)){RSORT.k='net';RSORT.dir=-1;}
 document.getElementById('rhint').textContent=detail?'':'income · margin · velocity fill in after this market’s next scan';
 document.getElementById('rth').innerHTML=cols.map(([k,l])=>
  '<th class="sort" data-k="'+k+'">'+l+(RSORT.k===k?'<span class="ar">'+(RSORT.dir<0?'▼':'▲')+'</span>':'')+'</th>').join('');
 document.querySelectorAll('#rth .sort').forEach(th=>th.onclick=()=>{const k=th.dataset.k;
  if(RSORT.k===k)RSORT.dir*=-1;else{RSORT.k=k;RSORT.dir=(k==='item')?1:-1;}renderReport();});
 const q=(document.getElementById('rq').value||'').toLowerCase(),f=document.getElementById('rf').value;
 let rows=its.filter(x=>(x.item||'').toLowerCase().includes(q));
 if(f==='income')rows=rows.filter(x=>(x.net||0)>0);
 if(f==='expense')rows=rows.filter(x=>(x.net||0)<0);
 const getv=RCOLS.find(c=>c[0]===RSORT.k)[2];
 rows.sort((a,b)=>{if(RSORT.k==='item')return RSORT.dir*String(a.item).localeCompare(String(b.item));
  return RSORT.dir*((getv(a)||0)-(getv(b)||0));});
 const cell=(x,k)=>{
  if(k==='item')return '<td>'+esc(x.item)+'</td>';
  if(k==='sold')return '<td class="num">'+fmt(x.sold)+'</td>';
  if(k==='bought')return '<td class="num">'+fmt(x.bought)+'</td>';
  if(k==='income')return '<td class="num">'+fmt(x.income)+'</td>';
  if(k==='expense')return '<td class="num" style="color:var(--down)">'+fmt(x.expense)+'</td>';
  if(k==='net')return '<td class="num" style="color:'+((x.net||0)>=0?'var(--up)':'var(--down)')+'">'+fmt(x.net)+'</td>';
  if(k==='mgn'){const m=(x.income>0)?(x.net/x.income*100):null;
   return m===null?'<td class="num faint">—</td>':'<td class="num" style="color:'+(m>=0?'var(--up)':'var(--down)')+'">'+m.toFixed(0)+'%</td>';}
  return '<td class="num muted">'+fmt(x.tsold)+'</td>';};
 document.getElementById('rtb').innerHTML=rows.map(x=>'<tr>'+cols.map(c=>cell(x,c[0])).join('')+'</tr>').join('')
  ||'<tr><td colspan="'+cols.length+'" class="faint" style="height:34px">No items match.</td></tr>';}
function renderFullness(items){
 const cap=items.reduce((a,x)=>a+(x.capacity||0),0),st=items.reduce((a,x)=>a+(x.stock||0),0);
 const avg=cap?Math.round(100*st/cap):0;
 const low=items.filter(x=>(x.capacity||0)>0&&(x.pct||0)<=20).length;
 const zero=items.filter(x=>(x.stock||0)<=0).length;
 document.getElementById('fstats').innerHTML=[
  ['Catalog items',items.length,''],
  ['Avg fullness','<span class="fill"><i style="width:'+Math.min(100,avg)+'%;background:'+fcol(avg)+'"></i></span>'+avg+'%','style="color:'+fcol(avg)+'"'],
  ['Low ≤20%',low,low>0?'style="color:var(--down)"':''],
  ['At 0 stock',zero,zero>0?'style="color:var(--amber)"':''],
  ['Shelf units',fmt(st),'']
 ].map(s=>'<div class="stat"><div class="k">'+s[0]+'</div><div class="v" '+(s[2]||'')+'>'+s[1]+'</div></div>').join('');}
function renderLowStock(items){
 const low=items.filter(x=>(x.capacity||0)>0&&(x.stock||0)<(x.capacity||0))
  .map(x=>({item:x.item,stock:x.stock||0,cap:x.capacity||0,pct:x.pct||0,need:Math.max(0,(x.capacity||0)-(x.stock||0))}))
  .sort((a,b)=>a.pct-b.pct).slice(0,15);
 document.getElementById('lsnote').textContent=low.length?('lowest '+low.length+' · click an item to prefill below'):'';
 document.getElementById('lstb').innerHTML=low.map(x=>{const p=Math.max(0,Math.min(100,x.pct));
  return '<tr><td><a class="pick" href="javascript:void 0" data-item="'+esc(x.item)+'" data-need="'+x.need+'" data-cap="'+x.cap+'">'+esc(x.item)+'</a></td>'+
   '<td class="num"><span class="fill"><i style="width:'+p+'%;background:'+fcol(p)+'"></i></span>'+
   '<span style="color:'+fcol(p)+'">'+Math.round(p)+'%</span></td>'+
   '<td class="num">'+fmt(x.stock)+'</td><td class="num">'+fmt(x.cap)+'</td>'+
   '<td class="num" style="color:var(--amber)">'+fmt(x.need)+'</td></tr>';}).join('')
  ||'<tr><td colspan="5" class="faint" style="height:34px">Everything is well stocked.</td></tr>';
 document.querySelectorAll('#lstb .pick').forEach(a=>a.onclick=()=>
  pickItem(a.dataset.item,+a.dataset.need||0,+a.dataset.cap||0));}
function pickItem(item,need,cap){
 // prefill the item into every item input + suggest the shortfall as restock qty / fill-to-cap stock
 ['si','ri','di'].forEach(id=>{const el=document.getElementById(id);if(el){el.value=item;el.classList.add('flash');setTimeout(()=>el.classList.remove('flash'),1200);}});
 const rq=document.getElementById('rqty');if(rq&&need)rq.value=need;
 const ss=document.getElementById('ss');if(ss&&cap)ss.value=cap;
 const ri=document.getElementById('ri');if(ri){try{ri.scrollIntoView({behavior:'smooth',block:'center'});}catch(e){}ri.focus();}}
['rmonth','rf'].forEach(id=>document.getElementById(id).onchange=renderReport);
document.getElementById('rq').oninput=renderReport;
document.getElementById('ls').onclick=async()=>{const m=document.getElementById('lmsg');m.textContent='saving…';
 const d=await post('/api/owner/set_loyalty',{market_id:mid(),pts_mult:+document.getElementById('lm').value||1,
  coin_bonus:+document.getElementById('lb').value||0,pct_bonus:+document.getElementById('lp').value||0}).catch(()=>({}));
 m.textContent=d.ok?'saved ✓':(d.error||'failed');};
document.getElementById('gen').onclick=async()=>{const m=document.getElementById('gmsg');m.textContent='working…';
 const d=await post('/api/owner/generate_orders',{market_id:mid()}).catch(()=>({}));
 m.textContent=d.ok?('created '+(d.created??'?')+' order(s)'):(d.error||'failed');};
document.getElementById('sset').onclick=async()=>{const m=document.getElementById('smsg');m.textContent='…';
 const b={market_id:mid(),item:document.getElementById('si').value.trim(),
  coin:+document.getElementById('sp').value||0};
 const st=document.getElementById('ss').value;if(st!=='')b.stock=+st;
 const d=await post('/api/owner/set_item',b).catch(()=>({}));
 m.textContent=d.ok?'set ✓':(d.error||'failed');};
document.getElementById('rlog').onclick=async()=>{const m=document.getElementById('rmsg');m.textContent='…';
 const d=await post('/api/owner/log_restock',{market_id:mid(),item:document.getElementById('ri').value.trim(),
  qty:+document.getElementById('rqty').value||0,cost:+document.getElementById('rc').value||0}).catch(()=>({}));
 m.textContent=d.ok?'logged ✓':(d.error||'failed');};
document.getElementById('del').onclick=async()=>{const m=document.getElementById('dmsg');
 const it=document.getElementById('di').value.trim();if(!it){m.textContent='enter an item';return;}
 m.textContent='removing…';
 const d=await post('/api/owner/remove_item',{market_id:mid(),item:it,mode:'full'}).catch(()=>({}));
 m.textContent=d.ok?'removed ✓':(d.error||'failed');};
// Reveal owner tools. Don't depend on the nav's async OWNERINFO landing within a fixed
// delay (that race left logged-in owners stuck on the locked screen) — fetch /api/me here
// and await it, so the check can't run early.
async function initOwner(){
 let me=window.OWNERINFO;
 if(!(me&&me.logged_in)){
  try{me=await (await fetch('/api/me',{cache:'no-store'})).json();window.OWNERINFO=me;}catch(e){}
 }
 if(me&&me.logged_in&&(me.owned||[]).length){
  owned=me.owned;
  document.getElementById('locked').style.display='none';
  document.getElementById('panel').style.display='';
  chips();loadMk();
 }else if(me&&me.logged_in){
  document.getElementById('locked').textContent=
   'Logged in as '+(me.name||'you')+" — but you don't own or manage any market yet.";
 }
}
window.addEventListener('load',initOwner);
// Collapsible panels — click any header to minimize it; remembered per panel across reloads.
(function(){
 document.querySelectorAll('.panel>.ph').forEach(ph=>{
  const panel=ph.parentElement;
  const cx=document.createElement('span');cx.className='cx';cx.textContent='▾';ph.insertBefore(cx,ph.firstChild);
  const tt=ph.querySelector('.t');const k='mmcol:'+((tt?tt.textContent:ph.textContent)||'').trim();
  try{if(localStorage.getItem(k)==='1')panel.classList.add('collapsed');}catch(e){}
  ph.addEventListener('click',e=>{
   if(e.target!==cx&&e.target.closest('select,input,a,button'))return;   // don't toggle when using a control
   panel.classList.toggle('collapsed');
   try{localStorage.setItem(k,panel.classList.contains('collapsed')?'1':'0');}catch(e){}
  });
 });
})();
</script></body></html>"""


async def _handle_mymarket_page(request):
    html = (_MYMARKET_HTML.replace("__TERMINAL_CSS__", _TERMINAL_CSS)
            .replace("__NAV__", _TERMINAL_NAV))
    return web.Response(text=html, content_type="text/html")


# ── /exchange — pro-terminal exchange view (XTB/IBKR-style, read-only) ────────
# Purely additive page: consumes the existing /api/stocks and /api/me endpoints.
# The order ticket places REAL trades via POST /api/trade (session + CSRF authed);
# it used to only estimate and hand you a /stock command to paste into Discord.
_EXCHANGE_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Abexilas Exchange</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0b0f10;--panel:#11171a;--panel2:#161d20;--row:#121a1c;--hover:#1a2427;--sel:#1c2a30;
--seam:#070b0b;--line:#212b2e;--line2:#2b3739;--ink:#d9e0e0;--ink2:#f0f4f4;--muted:#7a8a8a;--faint:#4b5a5a;
--up:#1fa97a;--down:#e5484d;--accent:#3f8fcf;--amber:#cfa637;
--sans:"IBM Plex Sans",-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:13px;-webkit-font-smoothing:antialiased}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
.up{color:var(--up)}.down{color:var(--down)}.muted{color:var(--muted)}.faint{color:var(--faint)}
header{display:flex;align-items:center;gap:20px;height:44px;padding:0 16px;border-bottom:1px solid var(--line);background:var(--panel)}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:14px;letter-spacing:.4px}
.brand .m{width:22px;height:22px;background:var(--up);color:#04120c;display:grid;place-items:center;font-weight:700;font-size:13px}
nav{display:flex;gap:2px;height:100%;margin-left:6px}
nav a{display:flex;align-items:center;padding:0 13px;color:var(--muted);font-weight:600;font-size:13px;cursor:pointer;
border-bottom:2px solid transparent;text-decoration:none}
nav a.on{color:var(--ink2);border-bottom-color:var(--accent)}nav a:hover{color:var(--ink)}
.rt{margin-left:auto;display:flex;align-items:center;gap:14px}
.rt .bp{text-align:right;line-height:1.15}.rt .bp b{font-size:13px}
.rt .bp span{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.grid{display:grid;grid-template-columns:262px 1fr 302px;gap:1px;background:var(--seam);min-height:calc(100vh - 44px)}
.col{background:var(--bg);min-width:0;display:flex;flex-direction:column;gap:1px}
.panel{background:var(--panel);border:1px solid var(--line)}
.ph{height:30px;display:flex;align-items:center;justify-content:space-between;padding:0 10px;background:var(--panel2);border-bottom:1px solid var(--line)}
.ph .t{font-size:10px;letter-spacing:.7px;text-transform:uppercase;color:var(--muted);font-weight:600}
.pb{padding:8px 10px}
table.w{width:100%;border-collapse:collapse;table-layout:fixed}
table.w th{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600;text-align:right;
padding:5px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel)}
table.w th:first-child{text-align:left}
table.w td{padding:0 8px;height:34px;border-bottom:1px solid var(--row);vertical-align:middle;white-space:nowrap;overflow:hidden}
table.w th:nth-child(2),table.w td:nth-child(2){width:42px;padding:0 4px}
table.w th:nth-child(3),table.w td:nth-child(3){width:56px}
table.w th:nth-child(4),table.w td:nth-child(4){width:58px}
table.w tr{cursor:pointer}table.w tr:hover td{background:var(--hover)}
table.w tr.sel td{background:var(--sel)}table.w tr.sel td:first-child{box-shadow:inset 2px 0 0 var(--accent)}
.tk{font-weight:600;font-size:12.5px;font-family:var(--mono)}
.nm{font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:120px}
td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:12px}
.chg{font-family:var(--mono);font-size:11.5px;font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:9.5px;font-weight:600;letter-spacing:.4px;padding:1px 5px;border:1px solid;border-radius:2px;font-family:var(--mono)}
.ihead{display:flex;align-items:center;gap:12px;padding:12px 14px;border-bottom:1px solid var(--line)}
.ihead .big{width:38px;height:38px;background:var(--panel2);border:1px solid var(--line2);display:grid;place-items:center;
font-family:var(--mono);font-weight:600;font-size:13px}
.ihead h1{margin:0;font-size:16px;font-weight:700;display:flex;align-items:center;gap:9px}
.ihead .sub{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
.ihead .px{margin-left:auto;text-align:right}
.ihead .px .v{font-family:var(--mono);font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.ihead .px .d{font-family:var(--mono);font-size:12px;font-weight:600;font-variant-numeric:tabular-nums}
.ranges{display:flex;padding:0 14px;border-bottom:1px solid var(--line)}
.ranges button{background:transparent;border:0;border-bottom:2px solid transparent;color:var(--muted);
font-family:var(--mono);font-weight:600;font-size:11px;padding:8px 12px;cursor:pointer}
.ranges button.on{color:var(--ink2);border-bottom-color:var(--accent)}
.chartwrap{position:relative;padding:8px 6px 6px}
svg.chart{width:100%;height:250px;display:block}
.tip{position:absolute;pointer-events:none;background:var(--panel2);border:1px solid var(--line2);padding:4px 8px;
font-size:11px;font-family:var(--mono);transform:translate(-50%,-135%);white-space:nowrap;opacity:0}
.stats{display:grid;grid-template-columns:repeat(6,1fr);border-top:1px solid var(--line)}
.stat{padding:9px 12px;border-right:1px solid var(--line)}.stat:last-child{border-right:0}
.stat .k{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600}
.stat .v{font-family:var(--mono);font-size:14px;font-weight:500;margin-top:4px;font-variant-numeric:tabular-nums}
.stat .s{font-size:10px;margin-top:1px;font-family:var(--mono)}
table.own{width:100%;border-collapse:collapse}
table.own th{font-size:9.5px;letter-spacing:.5px;text-transform:uppercase;color:var(--faint);font-weight:600;
padding:6px 12px;border-bottom:1px solid var(--line);text-align:right}
table.own th:first-child{text-align:left}
table.own td{padding:0 12px;height:30px;border-bottom:1px solid var(--row);font-size:12px}
table.own td.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
.dotc{width:8px;height:8px;display:inline-block;margin-right:8px;vertical-align:middle}
.field{margin-top:12px}
.lbl{font-size:10px;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);font-weight:600;display:block;margin-bottom:5px}
.inp{display:flex;align-items:center;gap:6px;background:var(--bg);border:1px solid var(--line2);padding:9px 11px}
.inp input{background:transparent;border:0;color:var(--ink2);font-family:var(--mono);font-size:16px;font-weight:600;width:100%;
outline:none;text-align:right;font-variant-numeric:tabular-nums}
.inp .u{color:var(--faint);font-family:var(--mono)}
.chips{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--seam);border:1px solid var(--line);margin-top:1px}
.chips button{background:var(--panel2);border:0;color:var(--muted);font-family:var(--mono);font-size:11px;padding:7px;cursor:pointer}
.chips button:hover{color:var(--ink);background:var(--hover)}
.kv{display:flex;justify-content:space-between;padding:5px 0;font-size:12px;color:var(--muted);border-bottom:1px solid var(--row)}
.kv b{color:var(--ink);font-family:var(--mono);font-weight:500;font-variant-numeric:tabular-nums}
.cmd{margin-top:12px;background:var(--bg);border:1px solid var(--line2);padding:9px 11px;font-family:var(--mono);
font-size:11.5px;color:var(--ink2);cursor:pointer;word-break:break-all}
.cmd:hover{border-color:var(--accent)}
.hint{font-size:10px;color:var(--faint);margin-top:5px}
.toggle{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--seam);border:1px solid var(--line)}
.toggle button{border:0;padding:10px;font-weight:700;font-size:12px;letter-spacing:.6px;text-transform:uppercase;cursor:pointer;
background:var(--panel2);color:var(--muted);font-family:var(--sans)}
.toggle button.buy.on{background:var(--up);color:#04120c}
.toggle button.sell.on{background:var(--down);color:#fff}
@media(max-width:1180px){.grid{grid-template-columns:1fr}}
</style></head>
<body>
<header>
  <div class="brand"><span class="m">A</span>ABEXILAS <span class="faint" style="font-weight:600">EXCHANGE</span></div>
  <nav><a href="/inventory">Inventory</a><a href="/ledger">Ledger</a><a class="on">Exchange</a><a href="/orders">Orders</a><a href="/teams">Teams</a><a href="/mymarket">My Market</a></nav>
  <div class="rt"><div class="bp"><b class="mono" id="hWho">—</b><br><span id="hWhoSub">not linked</span></div></div>
</header>
<div class="grid">
  <aside class="col"><div class="panel" style="flex:1">
    <div class="ph"><span class="t">Markets</span><span class="t mono" id="wCap"></span></div>
    <table class="w"><thead><tr><th>Symbol</th><th>Trend</th><th>Last</th><th>Chg%</th></tr></thead>
    <tbody id="list"></tbody></table>
  </div></aside>
  <section class="col">
    <div class="panel">
      <div class="ihead">
        <div class="big" id="mSym"></div>
        <div><h1><span id="mName"></span> <span class="tag" id="mGrade"></span></h1>
          <div class="sub"><span id="mTicker"></span> · Public market</div></div>
        <div class="px"><div class="v" id="mPrice"></div><div class="d" id="mChg"></div></div>
      </div>
      <div class="ranges" id="ranges">
        <button data-r="3600">1H</button><button data-r="86400">1D</button><button data-r="604800" class="on">1W</button>
        <button data-r="2592000">1M</button><button data-r="31536000">1Y</button><button data-r="0">ALL</button>
      </div>
      <div class="chartwrap"><svg class="chart" id="chart" preserveAspectRatio="none"></svg><div class="tip" id="tip"></div></div>
      <div class="stats" id="stats"></div>
    </div>
    <div class="panel">
      <div class="ph"><span class="t">Ownership · top holders</span><span class="t mono" id="ownSub"></span></div>
      <table class="own"><thead><tr><th>Holder</th><th>Shares</th><th>Value</th><th>Stake</th></tr></thead>
      <tbody id="ownBody"></tbody></table>
    </div>
  </section>
  <aside class="col">
    <div class="panel pb">
      <div class="toggle">
        <button class="buy on" id="btnBuy">Buy</button>
        <button class="sell" id="btnSell">Sell</button>
      </div>
      <div class="field"><span class="lbl">Amount (coins)</span>
        <div class="inp"><input id="amt" class="mono" value="100000"/><span class="u">¢</span></div>
        <div class="chips"><button data-a="50000">50K</button><button data-a="100000">100K</button>
          <button data-a="250000">250K</button><button data-a="1000000">1M</button></div>
      </div>
      <div style="margin-top:12px">
        <div class="kv">Shares<b id="sShares">—</b></div>
        <div class="kv">Price / share<b id="sPx">—</b></div>
        <div class="kv" style="border-bottom:0">Est. price impact<b id="sSlip">—</b></div>
      </div>
      <div class="cmd" id="cmd" title="Place this order">Buy</div>
      <div class="hint" id="tkHint">Log in (top right) to trade here.</div>
    </div>
    <div class="panel"><div class="ph"><span class="t">Your position</span></div><div class="pb" id="pos">
      <span class="faint">Link your Discord on the dashboard to see holdings.</span></div></div>
    <div class="panel"><div class="ph"><span class="t">Portfolio</span></div><div class="pb" id="pf">
      <span class="faint">—</span></div></div>
    <div class="panel" id="bondPanel" style="display:none">
      <div class="ph"><span class="t">Bonds</span><span class="faint" id="bondNote"></span></div>
      <div class="pb" id="bondList"></div>
    </div>
  </aside>
</div>
<script>
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmt=n=>Math.round(n).toLocaleString('en-US').replace(/,/g,' ');
const GC={AAA:'--up',AA:'--up',A:'--accent',BBB:'--accent',BB:'--amber',C:'--down'};
let MK=[],ME=null,cur=null,side='buy',rangeSec=604800;
function pathD(vals,w,h,pad){const mn=Math.min(...vals),mx=Math.max(...vals),rg=(mx-mn)||1;
 return vals.map((v,i)=>{const x=pad+i/((vals.length-1)||1)*(w-2*pad);const y=pad+(1-(v-mn)/rg)*(h-2*pad);
 return (i?'L':'M')+x.toFixed(1)+' '+y.toFixed(1);}).join(' ');}
function histSlice(m){const h=m.history||[];if(!h.length)return[m.price];
 if(!rangeSec)return h.map(p=>p.price);
 const cut=Date.now()/1000-rangeSec;
 const out=h.filter(p=>{const t=Date.parse(p.t)/1000;return !isFinite(t)||t>=cut;}).map(p=>p.price);
 return out.length>1?out:h.slice(-2).map(p=>p.price);}
function gtag(el,g){const c=css(GC[g]||'--faint');el.style.color=c;el.style.borderColor=c;el.style.background=c+'1c';el.textContent=g||'—';}
function drawMain(m){const el=document.getElementById('chart');const w=el.clientWidth||740,h=250,pad=12;
 el.setAttribute('viewBox','0 0 '+w+' '+h);
 const v=histSlice(m);const flat=Math.max(...v)===Math.min(...v);
 const col=flat?css('--faint'):(v[v.length-1]>=v[0]?css('--up'):css('--down'));
 let grid='';for(let i=0;i<5;i++){const y=pad+i/4*(h-2*pad);
  grid+='<line x1="'+pad+'" y1="'+y+'" x2="'+(w-pad)+'" y2="'+y+'" stroke="'+css('--line')+'" stroke-width="1"/>';}
 el.innerHTML=grid+'<path d="'+pathD(v,w,h,pad)+'" fill="none" stroke="'+col+'" stroke-width="1.4"/>' +
  '<line id="cx" y1="'+pad+'" y2="'+(h-pad)+'" stroke="'+css('--line2')+'" stroke-width="1" stroke-dasharray="2 3" style="opacity:0"/>'+
  '<circle id="dot" r="3" fill="'+col+'" style="opacity:0"/>';
 const tip=document.getElementById('tip'),dot=document.getElementById('dot'),cx=document.getElementById('cx');
 const mn=Math.min(...v),mx=Math.max(...v),rg=(mx-mn)||1;
 el.onmousemove=e=>{const r=el.getBoundingClientRect();let i=Math.round((e.clientX-r.left)/r.width*(v.length-1));
  i=Math.max(0,Math.min(v.length-1,i));const x=pad+i/((v.length-1)||1)*(w-2*pad),y=pad+(1-(v[i]-mn)/rg)*(h-2*pad);
  dot.setAttribute('cx',x);dot.setAttribute('cy',y);cx.setAttribute('x1',x);cx.setAttribute('x2',x);
  dot.style.opacity=1;cx.style.opacity=1;tip.style.left=(x/w*100)+'%';tip.style.top=(y/h*100)+'%';
  tip.style.opacity=1;tip.textContent=fmt(v[i])+' ¢';};
 el.onmouseleave=()=>{dot.style.opacity=0;cx.style.opacity=0;tip.style.opacity=0;};}
function render(){const m=MK.find(x=>x.mid===cur);if(!m)return;
 const up=m.pct>=0;
 document.getElementById('mSym').textContent=m.ticker||m.mid.toUpperCase().slice(0,4);
 document.getElementById('mName').textContent=m.name;
 gtag(document.getElementById('mGrade'),m.rating);
 document.getElementById('mTicker').textContent=m.ticker||m.mid;
 document.getElementById('mPrice').textContent=fmt(m.price)+' ¢';
 const c=document.getElementById('mChg');
 c.textContent=(up?'▲ ':'▼ ')+Math.abs(m.pct).toFixed(2)+'%';c.className='d '+(up?'up':'down');
 const q=m.quality||{};
 const S=[['Mkt cap',fmt(m.mcap)+' ¢'],['P/E',(+m.pe).toFixed(1)+'x'],
  ['Backing',(m.backing_pct??0)+'%',(m.backing_pct||0)>=(m.backing_target||50)?'up':'down'],
  ['Treasury',fmt(m.treasury)+' ¢'],['Holders',m.holders_count],
  ['Visitors/mo',fmt(q.visitors_month||0)]];
 document.getElementById('stats').innerHTML=S.map(s=>
  '<div class="stat"><div class="k">'+s[0]+'</div><div class="v">'+s[1]+'</div>'+
  (s[2]?'<div class="s '+s[2]+'">'+(s[2]=='up'?'≥ target':'&lt; target')+'</div>':'')+'</div>').join('');
 document.getElementById('ownSub').textContent=fmt(m.shares)+' shares';
 renderHolders(m);
 drawMain(m);
 document.querySelectorAll('#list tr').forEach(t=>t.classList.toggle('sel',t.dataset.k===cur));
 calc();renderPos();}
const CT={};
function holderRows(rows,shares){const cols=['--accent','--up','--amber','--down','--muted','--faint'];
 return rows.map((o,i)=>
  '<tr'+(o.you?' style="box-shadow:inset 2px 0 0 var(--up)"':'')+'><td><span class="dotc" style="background:'+css(cols[i%cols.length])+'"></span>'+
  (o.name||o.id)+(o.you?' <span style="font-size:9px;color:var(--up);font-weight:700">YOU</span>':'')+'</td>'+
  '<td class="num muted">'+fmt(o.shares)+'</td><td class="num muted">'+fmt(o.value)+'</td>'+
  '<td class="num" style="font-weight:600">'+(o.pct!=null?o.pct.toFixed(2):(shares?(o.shares/shares*100).toFixed(2):'0.00'))+'%</td></tr>').join('')
  ||'<tr><td colspan="4" class="faint" style="height:34px">No holders yet</td></tr>';}
async function renderHolders(m){const el=document.getElementById('ownBody');
 if(CT[m.mid]){el.innerHTML=holderRows(CT[m.mid],m.shares);return;}
 el.innerHTML=holderRows(m.top_holders||[],m.shares);   // instant fallback
 try{const r=await fetch('/api/exchange/captable?market_id='+encodeURIComponent(m.mid));
  const d=await r.json();
  if(d&&d.ok&&Array.isArray(d.rows||d.holders)){CT[m.mid]=(d.rows||d.holders);
   if(cur===m.mid)el.innerHTML=holderRows(CT[m.mid],m.shares);}}catch(e){}}
function renderList(){const tot=MK.reduce((a,m)=>a+m.mcap,0);
 document.getElementById('wCap').textContent=fmt(tot)+' ¢';
 document.getElementById('list').innerHTML=MK.map(m=>{const up=m.pct>=0;
  const hist=(m.history||[]).slice(-40).map(p=>p.price);const hv=hist.length>1?hist:[m.price,m.price];
  const flat=Math.max(...hv)===Math.min(...hv);
  const col=flat?css('--faint'):(up?css('--up'):css('--down'));
  return '<tr data-k="'+m.mid+'"><td><div class="tk">'+(m.ticker||m.mid)+'</div><div class="nm">'+m.name+'</div></td>'+
  '<td><svg width="42" height="22" viewBox="0 0 42 22" preserveAspectRatio="none"><path d="'+pathD(hv,42,22,2)+
  '" fill="none" stroke="'+col+'" stroke-width="1.2"/></svg></td>'+
  '<td class="num">'+fmt(m.price)+'</td>'+
  '<td class="num"><span class="chg '+(flat?'faint':(up?'up':'down'))+'">'+(flat?'—':((up?'+':'')+m.pct.toFixed(2)+'%'))+'</span></td></tr>';}).join('');
 document.querySelectorAll('#list tr').forEach(t=>t.onclick=()=>{cur=t.dataset.k;render();});}
function renderPos(){const m=MK.find(x=>x.mid===cur);const el=document.getElementById('pos');
 if(!ME||!ME.logged_in){el.innerHTML='<span class="faint">Link your Discord on the dashboard to see holdings.</span>';return;}
 const h=(ME.holdings||[]).find(x=>x.market===cur);
 el.innerHTML=h?('<div class="kv">Shares<b>'+fmt(h.shares)+'</b></div><div class="kv">Value<b>'+fmt(h.value)+' ¢</b></div>'+
  '<div class="kv" style="border-bottom:0">Cost basis<b>'+fmt(h.cost)+' ¢</b></div>')
  :('<span class="faint">No position in '+(m?m.name:'')+'.</span>');
 const pf=document.getElementById('pf');
 if(ME.holdings&&ME.holdings.length){const tot=ME.holdings.reduce((a,x)=>a+x.value,0);
  const cost=ME.holdings.reduce((a,x)=>a+x.cost,0);const pl=tot-cost;
  pf.innerHTML='<div class="kv" style="font-size:15px;color:var(--ink2)"><span></span><b>'+fmt(tot)+' ¢</b></div>'+
  '<div class="kv">Total P/L<b class="'+(pl>=0?'up':'down')+'">'+(pl>=0?'+':'')+fmt(pl)+' ¢</b></div>'+
  '<div class="kv" style="border-bottom:0">Positions<b>'+ME.holdings.length+'</b></div>';}
 else pf.innerHTML='<span class="faint">No positions.</span>';}
function calc(){const m=MK.find(x=>x.mid===cur);if(!m)return;
 const amt=+document.getElementById('amt').value||0,px=m.price||1;
 const sh=Math.floor(amt/px);
 document.getElementById('sShares').textContent=fmt(sh);
 document.getElementById('sPx').textContent=fmt(m.price)+' ¢';
 document.getElementById('sSlip').textContent=(side=='buy'?'+':'−')+(m.mcap?(amt/m.mcap*100).toFixed(2):'0.00')+'%';
 const btn=document.getElementById('cmd');
 btn.textContent=(side=='buy'?'Buy ':'Sell ')+fmt(Math.max(1,sh))+' '+(m.ticker||m.mid);
 btn.dataset.shares=Math.max(1,sh); btn.dataset.mid=m.mid;}
document.getElementById('btnBuy').onclick=()=>{side='buy';
 document.getElementById('btnBuy').classList.add('on');document.getElementById('btnSell').classList.remove('on');calc();};
document.getElementById('btnSell').onclick=()=>{side='sell';
 document.getElementById('btnSell').classList.add('on');document.getElementById('btnBuy').classList.remove('on');calc();};
document.getElementById('amt').oninput=calc;
document.querySelectorAll('.chips button').forEach(b=>b.onclick=()=>{document.getElementById('amt').value=b.dataset.a;calc();});
// Real order. The site used to only COPY a /stock command; sessions give us per-user
// auth, so the trade executes here. Server-side it is marshalled onto the bot's event
// loop so a web trade can't interleave with a Discord one.
document.getElementById('cmd').onclick=async()=>{
 const el=document.getElementById('cmd'), hint=document.getElementById('tkHint');
 const me=window.OWNERINFO;
 if(!me||!me.logged_in){hint.textContent='Log in (top right) to trade here.';return;}
 const sh=+el.dataset.shares||0, mid=el.dataset.mid;
 if(!mid||sh<1){hint.textContent='Pick a market and an amount first.';return;}
 if(!confirm((side=='buy'?'Buy ':'Sell ')+sh+' share(s) of '+mid+'?'))return;
 el.textContent='working…'; el.style.pointerEvents='none';
 try{
  const r=await fetch('/api/trade',{method:'POST',
    headers:{'Content-Type':'application/json','X-CSRF-Token':me.csrf||''},
    body:JSON.stringify({action:side,market_id:mid,shares:sh})});
  const j=await r.json();
  hint.textContent=(j&&j.message)?j.message:((j&&j.error)||'Trade failed.');
  el.textContent=(j&&j.ok)?'done ✓':'failed';
 }catch(e){hint.textContent='Network error.';el.textContent='failed';}
 setTimeout(()=>{el.style.pointerEvents='';calc();},1400);};
document.querySelectorAll('#ranges button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('#ranges button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 rangeSec=+b.dataset.r;const m=MK.find(x=>x.mid===cur);if(m)drawMain(m);});
addEventListener('resize',()=>{const m=MK.find(x=>x.mid===cur);if(m)drawMain(m);});
// Bond board. This lived on the retired /classic page, so deleting that page took away
// the only place bonds were visible. Rebuilt here — and unlike the old one it can BUY,
// through the same authed /api/trade path the share ticket uses.
function renderBonds(bonds){
 const panel=document.getElementById('bondPanel'), list=document.getElementById('bondList');
 if(!panel||!list)return;
 if(!bonds||!bonds.length){panel.style.display='none';return;}
 panel.style.display='';
 document.getElementById('bondNote').textContent=bonds.length+' series';
 list.innerHTML='';
 bonds.forEach(b=>{
  const covOk=(b.coverage||0)>=80, open=(b.status==='open');
  const row=document.createElement('div');
  row.style.cssText='padding:8px 0;border-bottom:1px solid var(--line)';
  row.innerHTML='<div style="display:flex;justify-content:space-between;gap:8px">'
   +'<b>'+esc(b.name)+' <span class="faint">#'+b.id+'</span></b>'
   +'<span class="'+(covOk?'up':'down')+'">'+(b.coverage!=null?b.coverage.toFixed(0)+'%':'-')+'</span></div>'
   +'<div class="faint" style="font-size:11px">'+esc(b.market_id)+' &middot; '+b.coupon_pct.toFixed(2)+'%/mo &middot; '
   +'matures '+esc(b.matures_at||'-')+' &middot; '+fmt(b.units_left)+' left @ '+fmt(Math.round(b.unit_price))+'c</div>';
  if(open){
   const wrap=document.createElement('div');
   wrap.style.cssText='display:flex;gap:6px;margin-top:6px';
   const inp=document.createElement('input');
   inp.type='number'; inp.min='1'; inp.value='1'; inp.className='mono';
   inp.style.cssText='width:70px;background:var(--panel2);border:1px solid var(--line2);color:var(--ink);padding:2px 6px';
   const btn=document.createElement('button');
   btn.textContent='Buy';
   btn.style.cssText='flex:1;background:var(--panel2);border:1px solid var(--line2);color:var(--ink);cursor:pointer;padding:2px 6px';
   btn.onclick=async()=>{
    if(!ME||!ME.logged_in){alert('Log in (top right) to buy bonds.');return;}
    const u=+inp.value||0; if(u<1)return;
    if(!confirm('Buy '+u+' unit(s) of '+b.name+'?'))return;
    btn.disabled=true; btn.textContent='...';
    try{
     const r=await fetch('/api/trade',{method:'POST',
       headers:{'Content-Type':'application/json','X-CSRF-Token':(ME&&ME.csrf)||''},
       body:JSON.stringify({action:'bond_buy',bond_id:b.id,units:u})});
     const j=await r.json();
     alert((j&&j.message)||(j&&j.error)||'Failed.');
     if(j&&j.ok){const s2=await (await fetch('/api/stocks')).json();renderBonds(s2.bonds||[]);}
    }catch(e){alert('Network error.');}
    btn.disabled=false; btn.textContent='Buy';};
   wrap.appendChild(inp); wrap.appendChild(btn); row.appendChild(wrap);
  }
  list.appendChild(row);});
}
async function boot(){
 try{const r=await fetch('/api/stocks');const d=await r.json();MK=d.markets||[];renderBonds(d.bonds||[]);}catch(e){MK=[];}
 try{const r=await fetch('/api/me');ME=await r.json();
  if(ME.logged_in){document.getElementById('hWho').textContent=ME.name||'linked';
   document.getElementById('hWhoSub').textContent='Discord linked';}}catch(e){}
 if(MK.length){cur=MK[0].mid;renderList();render();}
 else{document.getElementById('list').innerHTML='<tr><td colspan="4" class="faint" style="height:40px;padding:0 10px">No public markets yet</td></tr>';}
 setInterval(async()=>{try{const r=await fetch('/api/stocks');const d=await r.json();
  MK=d.markets||MK;renderList();render();renderBonds(d.bonds||[]);}catch(e){}},30000);}
boot();
</script></body></html>"""


async def _handle_exchange_page(request):
    return web.Response(text=_EXCHANGE_HTML, content_type="text/html")



def _owner_markets_web(uid) -> list:
    try:
        import Restocker_main as m
        return [str(x) for x in m._owner_markets_for_user(uid)]
    except Exception:
        return []


def _csrf_ok(request) -> bool:
    """State-changing owner POSTs must carry the session's CSRF token (defense in
    depth on top of SameSite=Lax). Read-only GETs do not need it."""
    sess = _session_user(request)
    if not sess:
        return False
    want = sess.get("csrf") or ""
    got = request.headers.get("X-CSRF-Token", "")
    return bool(want) and want == got


def _require_owner(request, market_id):
    """Return the session user_id IFF they're logged in AND own/manage market_id."""
    sess = _session_user(request)
    if not sess:
        return None
    uid = str(sess["user_id"])
    if str(market_id) not in _owner_markets_web(uid):
        return None
    return uid


async def _handle_owner_inventory(request):
    mid = (request.query.get("market_id") or "").strip()
    if not mid or not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized for this market."}, status=403)
    import Restocker_main as m
    try:
        inv = m._market_inventory(mid)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    raw = _load_markets() or {}
    name = (raw.get(mid, {}).get("name") if isinstance(raw.get(mid), dict) else None) or mid
    return web.json_response({"ok": True, "market_id": mid, "name": name, "items": inv})


async def _handle_owner_remove_item(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    mid = str(body.get("market_id") or "").strip()
    item = str(body.get("item") or "").strip()
    mode = str(body.get("mode") or "full").strip()
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    if not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized."}, status=403)
    if not item:
        return web.json_response({"ok": False, "error": "Missing item."}, status=400)
    import Restocker_main as m
    r = await m.run_on_bot_loop(m._remove_market_item, mid, item, adjust_totals=(mode != "hide"))
    _CACHE.clear()
    return web.json_response({"ok": True, **r})


async def _handle_owner_log_restock(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    mid = str(body.get("market_id") or "").strip()
    item = str(body.get("item") or "").strip()
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    if not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized."}, status=403)
    try:
        qty = int(body.get("qty", 0))
        cost = int(round(float(body.get("cost", 0))))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "qty/cost must be numbers."}, status=400)
    if not item or qty < 1:
        return web.json_response({"ok": False, "error": "Missing item or quantity."}, status=400)
    import Restocker_main as m
    r = await m.run_on_bot_loop(m._log_manual_restock, mid, item, qty, cost)
    _CACHE.clear()
    return web.json_response({"ok": True, **r})


async def _handle_owner_set_item(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    mid = str(body.get("market_id") or "").strip()
    item = str(body.get("item") or "").strip()
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    if not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized."}, status=403)
    if not item:
        return web.json_response({"ok": False, "error": "Missing item."}, status=400)
    coin = body.get("coin")
    stock = body.get("stock")
    import Restocker_main as m
    r = await m.run_on_bot_loop(m._set_market_item, mid, item, coin=coin, stock=stock)
    _CACHE.clear()
    return web.json_response({"ok": True, **r})


async def _handle_api_trade(request):
    """Buy/sell shares, or invest/redeem ABX Index units, as the logged-in user.

    The site was read-only ("no per-user trade auth") — sessions now give us that, so
    this is the website's half of retiring the /stock commands.

    THREADING: the web server runs on its own OS thread and event loop. The trade engine
    is only safe because every caller shares the bot's loop — its supply check and its
    writes are not atomic. So every mutation goes through run_on_bot_loop(), which is what
    keeps a web trade from interleaving with a Discord one.
    """
    import Restocker_main as m
    sess = _session_user(request)
    if not sess:
        return web.json_response({"ok": False, "error": "Log in first."}, status=401)
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    action = str(body.get("action") or "").strip().lower()
    uid = str(sess["user_id"])
    name = sess.get("name") or ""

    if action in ("buy", "sell"):
        mid = str(body.get("market_id") or "").strip()
        if not mid:
            return web.json_response({"ok": False, "error": "Which market?"}, status=400)
        try:
            shares = int(body.get("shares") or 0)
        except Exception:
            return web.json_response({"ok": False, "error": "shares must be a whole number."}, status=400)
        if not (1 <= shares <= 1_000_000):
            return web.json_response({"ok": False, "error": "shares must be 1..1,000,000."}, status=400)
        try:
            r = await m.run_on_bot_loop(m._do_stock_trade, action, uid, mid, shares, name)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"trade failed: {e}"}, status=500)
        return web.json_response({
            "ok": bool(r.get("ok")), "error": None if r.get("ok") else r.get("msg"),
            "message": r.get("msg"), "shares": r.get("shares"), "fill": r.get("fill"),
            "total": r.get("total"), "new_price": r.get("new_price"),
        })

    if action == "bond_buy":
        bid = str(body.get("bond_id") or "").strip()
        if not bid.isdigit():
            return web.json_response({"ok": False, "error": "bond_id must be numeric."}, status=400)
        try:
            units = int(body.get("units") or 0)
        except Exception:
            return web.json_response({"ok": False, "error": "units must be a whole number."}, status=400)
        if not (1 <= units <= 10_000_000):
            return web.json_response({"ok": False, "error": "units must be 1..10,000,000."}, status=400)
        try:
            r = await m.run_on_bot_loop(m._do_bond_buy, uid, int(bid), units, name)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"bond purchase failed: {e}"}, status=500)
        return web.json_response({
            "ok": bool(r.get("ok")), "message": r.get("msg"),
            "error": None if r.get("ok") else r.get("msg"),
            "cost": r.get("cost"), "units": r.get("units"),
            "coupon_monthly": r.get("coupon_monthly"), "coverage_pct": r.get("coverage_pct"),
        })

    if action == "invest_index":
        try:
            coins = int(body.get("coins") or 0)
        except Exception:
            return web.json_response({"ok": False, "error": "coins must be a whole number."}, status=400)
        if not (1 <= coins <= 1_000_000_000):
            return web.json_response({"ok": False, "error": "coins out of range."}, status=400)
        try:
            r = await m.run_on_bot_loop(m._etf_invest, uid, coins, name)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"invest failed: {e}"}, status=500)
        return web.json_response({"ok": bool(r.get("ok")), "message": r.get("msg"),
                                  "error": None if r.get("ok") else r.get("msg")})

    if action == "sell_index":
        units = body.get("units")
        if units in (None, "", "all"):
            units = "all"
        else:
            try:
                units = float(units)
            except Exception:
                return web.json_response({"ok": False, "error": "units must be a number or 'all'."}, status=400)
            if units <= 0:
                return web.json_response({"ok": False, "error": "units must be positive."}, status=400)
        try:
            r = await m.run_on_bot_loop(m._etf_redeem, uid, units, name)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"redeem failed: {e}"}, status=500)
        return web.json_response({"ok": bool(r.get("ok")), "message": r.get("msg"),
                                  "error": None if r.get("ok") else r.get("msg")})

    return web.json_response({"ok": False, "error": "action must be buy, sell, invest_index or sell_index."},
                             status=400)


async def _handle_owner_privacy(request):
    """Read (GET) or set (POST) whether this market's earnings appear publicly.

    Owner-controlled, because it's the owner's data. Hiding removes the market from the
    public Ledger and blocks its /report page for everyone except its own owner/managers.
    """
    import Restocker_db as db
    if request.method == "GET":
        mid = (request.query.get("market_id") or "").strip()
        if not mid or not _require_owner(request, mid):
            return web.json_response({"ok": False, "error": "Not authorized."}, status=403)
        return web.json_response({"ok": True, "market_id": mid,
                                  "public": not _earnings_hidden(mid)})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    mid = str(body.get("market_id") or "").strip()
    if not mid or not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized."}, status=403)
    make_public = bool(body.get("public"))
    db.set_config(f"market_earnings_public:{mid}", "1" if make_public else "0")
    _CACHE.pop("earnings", None)          # the Ledger caches for 8s; reflect this now
    _CACHE.pop("earnings_full", None)
    return web.json_response({"ok": True, "market_id": mid, "public": make_public})


async def _handle_owner_sales(request):
    """Day-by-day sales for a market, from the per-transaction CSN ledger.

    csn_history can only answer "what did this month total"; csn_transactions answers
    "what sold on Tuesday, and who bought it". Returns the daily series, the top
    customers, and — when ?day=YYYY-MM-DD is given — that day's item breakdown.
    """
    mid = (request.query.get("market_id") or "").strip()
    if not mid or not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized for this market."}, status=403)
    import Restocker_db as db
    try:
        days = max(1, min(int(request.query.get("days") or 30), 365))
    except Exception:
        days = 30
    try:
        daily = db.get_csn_daily_sales(mid, days)
        top = db.get_csn_top_customers(mid, days, 15)
        day = (request.query.get("day") or "").strip()
        detail = db.get_csn_day_detail(mid, day) if day else []
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    return web.json_response({
        "ok": True, "market_id": mid, "days": days,
        "daily": daily, "top_customers": top, "day": day, "day_items": detail,
        # Empty is a real state, not an error: the mod only started shipping the
        # per-transaction file recently, so tell the page to explain that.
        "has_data": bool(daily),
    })


async def _handle_owner_get_loyalty(request):
    """Read this market's restock-reward config (points multiplier + coin bonus)."""
    mid = (request.query.get("market_id") or "").strip()
    if not mid or not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized for this market."}, status=403)
    import json as _json, Restocker_db as db
    pm, cb, pct = 1.0, 0, 0.0
    try:
        raw = db.get_config(f"market_loyalty:{mid}")
        if raw:
            d = _json.loads(raw)
            pm = float(d.get("pts_mult", 1.0) or 1.0)
            cb = int(d.get("coin_bonus", 0) or 0)
            pct = float(d.get("pct_bonus", 0.0) or 0.0)
    except Exception:
        pass
    return web.json_response({"ok": True, "market_id": mid, "pts_mult": pm, "coin_bonus": cb, "pct_bonus": pct})


async def _handle_owner_set_loyalty(request):
    """Set this market's restock-reward config. Same store as the Discord /market loyalty
    command (bot_config key market_loyalty:<mid>), so both stay in sync."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    mid = str(body.get("market_id") or "").strip()
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    if not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized."}, status=403)
    try:
        pm = float(body.get("pts_mult", 1.0))
        cb = int(round(float(body.get("coin_bonus", 0))))
        pct = float(body.get("pct_bonus", 0.0))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Values must be numbers."}, status=400)
    if pm <= 0 or cb < 0 or pct < 0:
        return web.json_response({"ok": False, "error": "Multiplier must be > 0; bonuses must be ≥ 0."}, status=400)
    import json as _json, Restocker_db as db
    try:
        db.set_config(f"market_loyalty:{mid}",
                      _json.dumps({"pts_mult": pm, "coin_bonus": cb, "pct_bonus": pct}))
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    _CACHE.clear()
    return web.json_response({"ok": True, "pts_mult": pm, "coin_bonus": cb, "pct_bonus": pct})


async def _handle_owner_generate_orders(request):
    """Draft (and optionally create) restock orders for a market from its stock scan —
    refill every under-target item back up to target_percent. apply=false returns a preview."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    mid = str(body.get("market_id") or "").strip()
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    if not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized."}, status=403)
    try:
        target = float(body.get("target_percent", 80))
    except (TypeError, ValueError):
        target = 80.0
    if target <= 0 or target > 100:
        target = 80.0
    import Restocker_main as m
    try:
        to_order, skipped_active, at_target, skipped_guard = m._stock_refill_plan(mid, target)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    preview = [{"item": it, "qty": int(q)} for it, q, _ in to_order[:50]]
    if not bool(body.get("apply", False)):
        return web.json_response({"ok": True, "preview": True, "count": len(to_order),
                                  "skipped_active": skipped_active, "at_target": at_target,
                                  "skipped_guard": skipped_guard, "items": preview})
    try:
        created = await m.run_on_bot_loop(m._create_restock_orders, to_order, mid)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    _CACHE.clear()
    return web.json_response({"ok": True, "created": int(created),
                              "skipped_guard": skipped_guard, "items": preview})


async def _handle_owner_catalog(request):
    """Items grouped by category for the order-builder ('My Market' tab): stock, capacity,
    target %, tracked — powers the ticked-item restock builder."""
    mid = (request.query.get("market_id") or "").strip()
    if not mid or not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized for this market."}, status=403)
    import Restocker_main as m
    try:
        by_cat = m._market_catalog_by_category(mid)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    return web.json_response({"ok": True, "market_id": mid, "categories": by_cat})


async def _handle_owner_set_target(request):
    """Set (or partially update) one item's per-market restock target %/tracked flag.
    Either field may be omitted so ticking a box doesn't reset a tuned %."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    mid = str(body.get("market_id") or "").strip()
    item = str(body.get("item") or "").strip()
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    if not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized."}, status=403)
    if not item:
        return web.json_response({"ok": False, "error": "Missing item."}, status=400)
    raw_pct = body.get("target_pct")
    try:
        target_pct = None if raw_pct is None else max(0.0, min(100.0, float(raw_pct)))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "target_pct must be a number."}, status=400)
    raw_trk = body.get("tracked")
    tracked = None if raw_trk is None else bool(raw_trk)
    import Restocker_db as db
    try:
        db.set_market_item_target(mid, item, target_pct=target_pct, tracked=tracked)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    _CACHE.clear()
    return web.json_response({"ok": True})


async def _handle_owner_build_order(request):
    """Build restock orders from this market's ticked items, each refilled to its own tuned
    target %. apply=false returns a preview (same shape as generate_orders) without creating
    orders."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    mid = str(body.get("market_id") or "").strip()
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    if not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized."}, status=403)
    import Restocker_main as m, Restocker_db as db
    try:
        targets = db.get_market_item_targets(mid) or {}
        to_order, skipped_active, at_target, skipped_guard = m._stock_refill_plan(mid, item_targets=targets)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    preview = [{"item": it, "qty": int(q)} for it, q, _ in to_order[:50]]
    if not bool(body.get("apply", False)):
        return web.json_response({"ok": True, "preview": True, "count": len(to_order),
                                  "skipped_active": skipped_active, "at_target": at_target,
                                  "skipped_guard": skipped_guard, "items": preview})
    if not to_order:
        return web.json_response({"ok": True, "created": 0,
                                  "skipped_guard": skipped_guard, "items": []})
    try:
        created = await m.run_on_bot_loop(m._create_restock_orders, to_order, mid)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    _CACHE.clear()
    return web.json_response({"ok": True, "created": int(created),
                              "skipped_guard": skipped_guard, "items": preview})


async def _handle_owner_futures(request):
    """A logged-in market owner requests a (bulk) futures order for THEIR market from the
    website — pasted as a text list, same parser as the Discord modal. Saved pending and
    posted to the futures channel for a manager to Approve & Fulfill."""
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Bad or missing CSRF token."}, status=403)
    sess = _session_user(request)
    if not sess:
        return web.json_response({"ok": False, "error": "Log in first."}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    mid = str(body.get("market_id") or "").strip()
    if not mid or not _require_owner(request, mid):
        return web.json_response({"ok": False, "error": "Not authorized for this market."}, status=403)
    notes = str(body.get("notes") or "").strip()[:500]
    import Restocker_main as m, Restocker_db as _db
    # Preferred: structured lines picked from the catalog on the website ({item, qty} dicts).
    # These validate against the catalog and arrive pre-linked (item_key) for consignment
    # pricing. Fallback: a pasted text blob (the Discord modal's format), parsed line-by-line.
    parsed = []
    raw_lines = body.get("lines") if isinstance(body.get("lines"), list) else None
    if raw_lines:
        catalog = _cached("items", _load_items) or {}
        cat_lookup = {str(k).strip().lower(): str(k) for k in catalog.keys()}
        unknown = []
        for it in raw_lines[:60]:              # ≥ the build_order preview's 50-line slice
            if not isinstance(it, dict):
                continue
            name = str(it.get("item") or "").strip()
            try:
                qty = max(1, min(100000, int(it.get("qty") or 0)))
            except (TypeError, ValueError):
                continue
            real = cat_lookup.get(name.lower())
            if not real:
                unknown.append(name)
                continue
            parsed.append({"item": real, "qty": qty, "unit": "pieces",
                           "raw": f"web:{real} x{qty}", "item_key": real})
        if unknown:
            return web.json_response({"ok": False,
                                      "error": "Not in the catalog: " + ", ".join(unknown[:5])})
    else:
        parsed = m._parse_futures_bulk_text(str(body.get("items") or ""))
    if not parsed:
        return web.json_response({"ok": False,
                                  "error": "Add at least one catalog item (or paste one per line)."})
    uid = str(sess.get("user_id") or "")
    uname = sess.get("name") or "Web owner"
    try:
        bulk_id = _db.create_futures_bulk(uid, uname, mid, uid, notes)
        for p in parsed:
            _db.add_futures_bulk_line(bulk_id, p["item"], p["qty"], p.get("unit", "pieces"),
                                      "", p.get("raw", ""), item_key=p.get("item_key"))
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    # Post the manager review card on the bot loop (fire-and-forget), same pattern as web orders.
    try:
        loop = getattr(m, "_BOT_LOOP", None)
        if loop is not None:
            import asyncio as _a
            from views.web import post_futures_bulk_review
            _a.run_coroutine_threadsafe(post_futures_bulk_review(bulk_id), loop)
    except Exception as e:
        print(f"⚠️ web futures #{bulk_id} notify failed: {e}")
    _CACHE.clear()
    return web.json_response({"ok": True, "bulk_id": bulk_id, "count": len(parsed),
                              "items": [{"item": p["item"], "qty": p["qty"],
                                         "unit": p.get("unit", "pieces")} for p in parsed]})


async def _handle_owner_futures_bills(request):
    """The logged-in user's consignment bills: every futures deal where THEY are the customer,
    with upfront / margin-owed-so-far (from their CSN resales) / paid / remaining. Keyed to the
    session user, so it can only ever show someone their own debt."""
    sess = _session_user(request)
    if not sess:
        return web.json_response({"ok": False, "error": "Log in first."}, status=401)
    uid = str(sess.get("user_id") or "")
    import Restocker_main as m, Restocker_db as _db
    out = []
    try:
        for b in _db.list_futures_bulk(customer_id=uid, limit=25):
            if str(b.get("status")) in ("declined", "cancelled"):
                continue
            full = _db.get_futures_bulk(b["id"])
            o = m._futures_bulk_owed(full)
            lines = []
            for l in o["lines"]:
                if not l.get("priced"):
                    continue
                try:
                    disp = m._pretty_item_name(l.get("item") or "")
                except Exception:
                    disp = l.get("item") or ""
                lines.append({"item": disp, "qty": l["qty"], "resold": l["resold"],
                              "owed": l["owed"]})
            out.append({"id": b["id"], "market_id": b.get("market_id") or "",
                        "status": b.get("status"), "created_at": b.get("created_at"),
                        "upfront": o["upfront"], "owed": o["owed_so_far"],
                        "paid": o["paid"], "remaining": o["remaining"],
                        "unpriced": o["unpriced"], "lines": lines})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    return web.json_response({"ok": True, "deals": out})


async def _handle_exchange_captable(request):
    """Live cap table for one public market (the web version of the old /stock holders):
    ranked holders with %, value at the current mark, your stake, and free float. Names
    follow the same privacy rules as the public leaderboard — anonymized unless the holder
    opted in — EXCEPT: you always see yourself, and the market's owner/manager sees real
    names (matching the old owner-gated Discord command)."""
    mid = (request.query.get("market_id") or "").strip()
    if not mid:
        return web.json_response({"ok": False, "error": "market_id required"}, status=400)
    import Restocker_db as db
    try:
        listing = db.get_market_shares(mid)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    if not listing or not listing.get("active"):
        return web.json_response({"ok": False, "error": "not a public market"})
    price = float(listing.get("share_price") or 0)
    outstanding = float(listing.get("shares_outstanding") or 0)
    holders = sorted(db.get_holders(mid) or [],
                     key=lambda h: -float(h.get("shares") or 0))
    sess = _session_user(request)
    uid = str(sess.get("user_id")) if sess else ""
    privileged = bool(uid) and mid in _owner_markets_web(uid)

    holder_names = {}
    if _YAML_AVAILABLE:
        try:
            with open(_resolve_data_file("stock_names.yml"), encoding="utf-8") as f:
                holder_names = _yaml.safe_load(f) or {}
        except Exception:
            pass
    prefs = _user_prefs()

    def _label(huid: str) -> str:
        real = holder_names.get(huid)
        if not real:
            try:
                real = db.get_ign(huid)
            except Exception:
                real = None
        if huid == uid:
            return real or (sess.get("name") if sess else None) or ("…" + huid[-4:])
        if privileged:
            return real or ("…" + huid[-4:])
        if prefs.get(huid, {}).get("anonymous", True):
            return "…" + huid[-4:]
        return real or ("…" + huid[-4:])

    rows, held_total, your_shares = [], 0.0, 0.0
    for i, h in enumerate(holders, 1):
        huid = str(h.get("user_id"))
        sh = float(h.get("shares") or 0)
        if sh <= 0:
            continue
        held_total += sh
        if huid == uid:
            your_shares = sh
        pct = (100.0 * sh / outstanding) if outstanding > 0 else 0.0
        rows.append({"rank": i, "name": _label(huid), "shares": sh,
                     "pct": round(pct, 2), "value": round(sh * price),
                     "you": huid == uid})
    mname = mid
    try:
        raw = _load_markets() or {}
        info = raw.get(mid)
        if isinstance(info, dict):
            mname = info.get("name") or mid
    except Exception:
        pass
    try:  # company label wins on the cap table — the stock is the company, not the shop
        lbl = str(db.get_config(f"stock_label:{mid}") or "").strip()
        if lbl:
            mname = lbl
    except Exception:
        pass
    return web.json_response({"ok": True, "market_id": mid, "name": mname,
                              "ticker": listing.get("ticker") or "",
                              "price": price, "outstanding": outstanding,
                              "mktcap": round(price * outstanding),
                              "holders": len(rows), "held_total": held_total,
                              "free_float": max(0.0, outstanding - held_total),
                              "your_shares": your_shares,
                              "your_pct": round(100.0 * your_shares / outstanding, 2) if outstanding > 0 else 0,
                              "your_value": round(your_shares * price),
                              "logged_in": bool(uid), "privileged": privileged,
                              "rows": rows})


async def _handle_api_investors(request):
    """Public investor register (the GEX.PR preferred shareholders): entity name, preferred
    shares, share % and total profit-share received. The cap table is already public on the
    Crimson Banking server, so names here aren't a privacy leak; coin balances are NOT
    exposed — only the profit-share totals."""
    import Restocker_db as db
    try:
        invs = sorted((db.get_investors() or {}).values(),
                      key=lambda i: -float(i.get("share_pct") or 0))
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    import Restocker_main as m
    return web.json_response({"ok": True, "pool_pct": m._investor_pool_pct(),
                              "investors": [{
                                  "name": i.get("name") or ("…" + str(i.get("user_id"))[-4:]),
                                  "pref_shares": float(i.get("pref_shares") or 0),
                                  "share_pct": float(i.get("share_pct") or 0),
                                  "total_received": float(i.get("total_received") or 0),
                              } for i in invs if float(i.get("share_pct") or 0) > 0]})


async def _handle_api_order(request):
    """A logged-in customer places an order from the website (catalog items only, multi-item
    cart). Saved to web_orders and posted to the web-orders Discord channel for the normal
    manager approve/decline flow. Every order carries the customer's linked Discord ID."""
    sess = _session_user(request)
    if not sess:
        return web.json_response(
            {"ok": False, "error": "Log in first — run /website_login in Discord to link your account."},
            status=401)
    if not _csrf_ok(request):
        return web.json_response({"ok": False, "error": "Session expired — reload the page and try again."},
                                 status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_items = body.get("items") if isinstance(body.get("items"), list) else []
    notes = str(body.get("notes") or "").strip()[:500]
    if not raw_items:
        return web.json_response({"ok": False, "error": "Your cart is empty."})

    catalog = _cached("items", _load_items) or {}
    cat_lookup = {str(k).strip().lower(): str(k) for k in catalog.keys()}
    items, unknown = [], []
    for it in raw_items[:40]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("item") or it.get("name") or "").strip()
        try:
            qty = int(it.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if not name or qty <= 0:
            continue
        real = cat_lookup.get(name.lower())
        if not real:
            unknown.append(name)
            continue
        items.append({"name": real, "qty": min(qty, 100000)})
    if unknown:
        return web.json_response({"ok": False, "error": "Not in the catalog: " + ", ".join(unknown[:5])})
    if not items:
        return web.json_response({"ok": False, "error": "Add at least one catalog item with a quantity."})

    username   = sess.get("name") or "Web customer"
    discord_id = str(sess.get("user_id") or "")
    try:
        import Restocker_db as _db
        order_id = _db.save_web_order(discord_username=username, discord_id=discord_id,
                                      items=items, notes=notes)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"Couldn't save your order: {e}"}, status=500)

    # Post the Discord approve/decline notification on the bot's own loop (fire-and-forget).
    try:
        notify = globals().get("_order_notify_fn")
        import Restocker_main as _m
        loop = getattr(_m, "_BOT_LOOP", None)
        if notify is not None and loop is not None:
            import asyncio as _a
            _a.run_coroutine_threadsafe(notify(order_id, username, items, notes), loop)
    except Exception as e:
        print(f"⚠️ web order #{order_id} notify failed: {e}")

    return web.json_response({"ok": True, "order_id": order_id, "count": len(items)})


def _network_secret_ok(request) -> bool:
    """Shared-secret auth for the satellite bot's /api/network/* calls. If V Helper has
    no NETWORK_SHARED_SECRET set, the network API stays closed."""
    try:
        import Restocker_main as _m
        want = str(getattr(_m, "NETWORK_SHARED_SECRET", "") or "")
    except Exception:
        return False
    got = request.headers.get("X-Network-Secret", "")
    return bool(want) and got == want


async def _handle_network_orders(request):
    """Satellite bot pulls the current open-order list to post in partner servers."""
    if not _network_secret_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    import Restocker_main as _m
    try:
        orders = _m._network_open_orders()
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    return web.json_response({"ok": True, "orders": orders})


async def _handle_network_claim(request):
    """Satellite bot reports that a worker in a partner server claimed an order."""
    if not _network_secret_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        oid = int(body.get("order_id") or 0)
    except (TypeError, ValueError):
        oid = 0
    wid   = str(body.get("worker_id") or "").strip()
    wname = str(body.get("worker_name") or "worker").strip()[:64]
    gid   = str(body.get("source_guild_id") or "").strip()
    if not oid or not wid:
        return web.json_response({"ok": False, "error": "order_id and worker_id are required"})

    import Restocker_main as _m
    try:
        res = await _m.run_on_bot_loop(_m._record_network_claim, oid, wid, wname, gid)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    # Fire-and-forget ping to the home worker channel (Discord I/O on the bot loop).
    if res.get("ok"):
        try:
            loop = getattr(_m, "_BOT_LOOP", None)
            if loop is not None:
                import asyncio as _a
                _a.run_coroutine_threadsafe(
                    _m._notify_network_claim(oid, wid, wname, gid), loop)
        except Exception as e:
            print(f"⚠️ network claim notify failed: {e}")
    return web.json_response(res)


# ── Land Exchange network API (the "V Tech Lands & Auctions" satellite) ──────────
async def _handle_network_land_listings(request):
    """Satellite pulls the current active land listings to render as a board."""
    if not _network_secret_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    import Restocker_main as _m
    try:
        listings = _m._network_land_listings()
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    return web.json_response({"ok": True, "listings": listings})


def _land_body_fields(body):
    try:
        lid = int(body.get("listing_id") or 0)
    except (TypeError, ValueError):
        lid = 0
    uid   = str(body.get("bidder_id") or body.get("buyer_id") or "").strip()
    uname = str(body.get("bidder_name") or body.get("buyer_name") or "member").strip()[:64]
    gid   = str(body.get("source_guild_id") or "").strip()
    return lid, uid, uname, gid


async def _handle_network_land_bid(request):
    """Satellite relays a bid placed in a partner server. Escrow runs on the bot loop."""
    if not _network_secret_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    lid, uid, uname, gid = _land_body_fields(body)
    try:
        amount = float(body.get("amount")) if body.get("amount") not in (None, "") else None
    except (TypeError, ValueError):
        amount = None
    if not lid or not uid:
        return web.json_response({"ok": False, "error": "listing_id and bidder_id are required"})

    import Restocker_main as _m
    try:
        res = await _m.run_on_bot_loop(_m._record_network_land_bid, lid, uid, uname, gid, amount)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    if res.get("ok"):
        try:
            loop = getattr(_m, "_BOT_LOOP", None)
            if loop is not None:
                import asyncio as _a
                note = (f"💰 Network bid on **#{lid}**: `{int(res.get('amount') or 0):,}` 🪙 "
                        f"from `{uname}`" + (" · ⏱️ anti-snipe extended" if res.get("anti_snipe_extended") else ""))
                _a.run_coroutine_threadsafe(_m._notify_network_land(lid, note, res), loop)
        except Exception as e:
            print(f"⚠️ network land bid notify failed: {e}")
    return web.json_response(res)


async def _handle_network_land_buy(request):
    """Satellite relays an instant-buy placed in a partner server."""
    if not _network_secret_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    lid, uid, uname, gid = _land_body_fields(body)
    if not lid or not uid:
        return web.json_response({"ok": False, "error": "listing_id and buyer_id are required"})

    import Restocker_main as _m
    try:
        res = await _m.run_on_bot_loop(_m._record_network_land_buy, lid, uid, uname, gid)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    if res.get("ok"):
        try:
            loop = getattr(_m, "_BOT_LOOP", None)
            if loop is not None:
                import asyncio as _a
                note = f"🏡 **#{lid}** bought via the network by `{uname}` for `{int(res.get('price') or 0):,}` 🪙."
                _a.run_coroutine_threadsafe(_m._notify_network_land(lid, note, res), loop)
        except Exception as e:
            print(f"⚠️ network land buy notify failed: {e}")
    return web.json_response(res)


async def _handle_network_land_create(request):
    """Satellite's /sell — create a listing. Writes run on the bot loop."""
    if not _network_secret_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    seller = str(body.get("seller_id") or "").strip()
    gid = str(body.get("source_guild_id") or "").strip()
    if not seller or not (body.get("title") and body.get("starting_price") is not None):
        return web.json_response({"ok": False, "error": "seller_id, title and starting_price are required"})
    import Restocker_main as _m
    try:
        res = await _m.run_on_bot_loop(_m._record_network_land_create, seller, gid, body)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    return web.json_response(res)


async def _handle_network_land_cancel(request):
    """Satellite's /cancel — seller/manager cancels a bid-free listing."""
    if not _network_secret_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    lid, uid, uname, gid = _land_body_fields(body)
    is_mgr = bool(body.get("is_manager"))
    if not lid or not uid:
        return web.json_response({"ok": False, "error": "listing_id and requester_id are required"})
    import Restocker_main as _m
    try:
        res = await _m.run_on_bot_loop(_m._record_network_land_cancel, lid, uid, is_mgr)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    if res.get("ok"):
        try:
            loop = getattr(_m, "_BOT_LOOP", None)
            if loop is not None:
                import asyncio as _a
                _a.run_coroutine_threadsafe(
                    _m._notify_network_land(lid, f"🚫 Listing **#{lid}** cancelled."), loop)
        except Exception:
            pass
    return web.json_response(res)


async def _handle_network_land_close(request):
    """Satellite's manager /close — force-settle or refund. Deal room opens via notify."""
    if not _network_secret_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        lid = int(body.get("listing_id") or 0)
    except (TypeError, ValueError):
        lid = 0
    refund = bool(body.get("refund_bidder"))
    if not lid:
        return web.json_response({"ok": False, "error": "listing_id is required"})
    import Restocker_main as _m
    try:
        res = await _m.run_on_bot_loop(_m._record_network_land_close, lid, refund)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    if res.get("ok"):
        try:
            loop = getattr(_m, "_BOT_LOOP", None)
            if loop is not None:
                import asyncio as _a
                note = f"🔨 Listing **#{lid}** closed by a manager ({res.get('outcome')})."
                _a.run_coroutine_threadsafe(_m._notify_network_land(lid, note, res), loop)
        except Exception:
            pass
    return web.json_response(res)


async def _handle_network_land_config(request):
    """Satellite's manager /config — GET current knobs (empty body) or set them."""
    if not _network_secret_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    updates = body.get("updates") if isinstance(body.get("updates"), dict) else None
    import Restocker_main as _m
    try:
        cfg = await _m.run_on_bot_loop(_m._network_land_config, updates)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    return web.json_response({"ok": True, "config": cfg})


def start_webserver_thread(port: int = 8080):
    """Run the aiohttp server in its OWN OS thread + event loop so dashboard
    traffic can't stall the Discord bot's gateway loop. State-mutating endpoints
    marshal their writes back to the bot loop via Restocker_main.run_on_bot_loop()."""
    import threading
    import asyncio as _a

    def _run():
        loop = _a.new_event_loop()
        _a.set_event_loop(loop)
        try:
            loop.run_until_complete(start_webserver(port))
        except Exception as e:
            print(f"⚠️  web server thread stopped: {e}", flush=True)

    threading.Thread(target=_run, name="webserver", daemon=True).start()


async def start_webserver(port: int = 8080):
    """Start the web server as a long-running background coroutine."""
    if not _AIOHTTP_AVAILABLE:
        print("⚠️  aiohttp not installed — web server disabled. Run: pip install aiohttp")
        return

    import time as _t

    @web.middleware
    async def _rate_limit_mw(request, handler):
        if not request.path.startswith("/api/bank/"):
            ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                  or (request.remote or "unknown"))
            now = _t.time()
            global _last_throttle_sweep
            if now - _last_throttle_sweep > 60:
                _last_throttle_sweep = now
                for _d in (_REQ_HITS, _LINK_ATTEMPTS):
                    for _ip in list(_d.keys()):
                        if not any(now - _ts < 60 for _ts in _d.get(_ip, [])):
                            _d.pop(_ip, None)
            recent = [t for t in _REQ_HITS.get(ip, []) if now - t < 60]
            if len(recent) >= 120:
                return web.json_response({"error": "rate limited"}, status=429)
            recent.append(now)
            _REQ_HITS[ip] = recent
        return await handler(request)

    app = web.Application(middlewares=[_rate_limit_mw])
    # Terminal redesign: every section is its own page. The old SPA stays at
    # /classic as a fallback until the new pages are proven in production.
    app.router.add_get("/",              _handle_inventory_page)
    app.router.add_get("/classic",       _handle_index)
    app.router.add_get("/inventory",     _handle_inventory_page)
    app.router.add_get("/ledger",        _handle_ledger_page)
    app.router.add_get("/orders",        _handle_orders_page)
    app.router.add_get("/teams",         _handle_teams_page)
    app.router.add_get("/mymarket",      _handle_mymarket_page)
    app.router.add_get("/api/items",     _handle_api_items)
    app.router.add_get("/api/markets",   _handle_api_markets)
    app.router.add_get("/api/earnings",  _handle_api_earnings)
    app.router.add_get("/api/earnings_full", _handle_api_earnings_full)
    app.router.add_get("/api/prices",    _handle_api_prices)
    app.router.add_get("/api/stocks",    _handle_api_stocks)
    app.router.add_post("/api/link",     _handle_api_link)
    app.router.add_get("/api/me",        _handle_api_me)
    app.router.add_post("/api/anon",     _handle_api_anon)
    app.router.add_post("/api/logout",   _handle_api_logout)
    app.router.add_get("/api/owner/inventory",   _handle_owner_inventory)
    app.router.add_post("/api/owner/remove_item", _handle_owner_remove_item)
    app.router.add_post("/api/owner/log_restock", _handle_owner_log_restock)
    app.router.add_post("/api/owner/set_item",    _handle_owner_set_item)
    app.router.add_post("/api/trade",             _handle_api_trade)
    app.router.add_get("/api/owner/sales",         _handle_owner_sales)
    app.router.add_get("/api/owner/privacy",       _handle_owner_privacy)
    app.router.add_post("/api/owner/privacy",      _handle_owner_privacy)
    app.router.add_get("/api/owner/loyalty",       _handle_owner_get_loyalty)
    app.router.add_post("/api/owner/set_loyalty",  _handle_owner_set_loyalty)
    app.router.add_post("/api/owner/generate_orders", _handle_owner_generate_orders)
    app.router.add_get("/api/owner/catalog",       _handle_owner_catalog)
    app.router.add_post("/api/owner/set_target",   _handle_owner_set_target)
    app.router.add_post("/api/owner/build_order",  _handle_owner_build_order)
    app.router.add_post("/api/owner/futures",      _handle_owner_futures)
    app.router.add_get("/api/owner/futures_bills", _handle_owner_futures_bills)
    app.router.add_get("/api/exchange/captable",   _handle_exchange_captable)
    app.router.add_get("/api/investors",           _handle_api_investors)
    app.router.add_post("/api/order",    _handle_api_order)
    app.router.add_get("/api/network/orders", _handle_network_orders)
    app.router.add_post("/api/network/claim", _handle_network_claim)
    app.router.add_get("/api/network/land/listings", _handle_network_land_listings)
    app.router.add_post("/api/network/land/bid", _handle_network_land_bid)
    app.router.add_post("/api/network/land/buy", _handle_network_land_buy)
    app.router.add_post("/api/network/land/create", _handle_network_land_create)
    app.router.add_post("/api/network/land/cancel", _handle_network_land_cancel)
    app.router.add_post("/api/network/land/close", _handle_network_land_close)
    app.router.add_post("/api/network/land/config", _handle_network_land_config)
    app.router.add_get("/report/{market}/{month}", _handle_report)
    app.router.add_get("/report/{market}",         _handle_report)
    app.router.add_get("/shares/{market}",         _handle_shares)
    app.router.add_get("/exchange",      _handle_exchange_page)
    app.router.add_get("/health",        _handle_health)

    try:
        import bank_api
        bank_api.register_bank_routes(app)
    except Exception as _e:
        print(f"⚠️  Bank API not registered: {_e}")

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site4 = web.TCPSite(runner, "0.0.0.0", port)
    site6 = web.TCPSite(runner, "::", port)
    await site4.start()
    try:
        await site6.start()
    except Exception:
        pass
    print(f"🌐  Web server running on http://0.0.0.0:{port}")
    print("     Endpoints: /  /api/items  /api/markets  /api/earnings  /api/prices  /api/stocks  /health")

    try:
        while True:
            import asyncio as _asyncio
            await _asyncio.sleep(3600)
    except Exception:
        await runner.cleanup()
