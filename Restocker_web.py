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


def _load_orders() -> list:
    try:
        import Restocker_db as db
        orders = db.load_orders()
        out = []
        for o in orders:
            out.append({
                "id":        o.get("id"),
                "shop":      o.get("shop", "?"),
                "item":      o.get("item", "?"),
                "requested": o.get("requested", 0),
                "produced":  o.get("produced", 0),
                "status":    o.get("status", "open"),
                "claimed_by": o.get("claimed_by") or "",
                "created_at": (o.get("created_at") or "")[:16],
                "claims_count": len(o.get("claims", [])),
            })
        return out
    except Exception:
        pass
    if _YAML_AVAILABLE:
        try:
            with open(_resolve_data_file("orders.yml"), encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
            orders = data.get("orders", []) or []
            out = []
            for o in (orders if isinstance(orders, list) else orders.values()):
                if not isinstance(o, dict):
                    continue
                out.append({
                    "id":        o.get("id", ""),
                    "shop":      o.get("shop", "?"),
                    "item":      o.get("item", "?"),
                    "requested": o.get("requested", 0),
                    "produced":  o.get("produced", 0),
                    "status":    o.get("status", "open"),
                    "claimed_by": o.get("claimed_by") or "",
                    "created_at": (o.get("created_at") or "")[:16],
                    "claims_count": 0,
                })
            return out
        except Exception:
            pass
    return []


def _load_all_earnings() -> dict:
    """Return {market_id: [month_dicts]} for all markets with CSN history.
    Reads from the DB (single source of truth); YAML files are a legacy fallback."""
    markets = _load_markets()

    def _parse_hist(data: dict, market_id: str) -> list:
        months_raw = (data or {}).get("months", {}) or {}
        out = []
        for mk, md in sorted(months_raw.items()):
            if not isinstance(md, dict):
                continue
            items_agg: dict = {}
            for iname, iv in (md.get("items") or {}).items():
                if not isinstance(iv, dict):
                    continue
                e = items_agg.setdefault(iname, {"sold": 0, "bought": 0, "net": 0})
                e["sold"]   += int(iv.get("sold_qty", 0))
                e["bought"] += int(iv.get("bought_qty", 0))
                e["net"]    += int(round(float(iv.get("net_coins", 0) or 0)))
            out.append({
                "month":  mk,
                "label":  md.get("label", mk),
                "income": int(md.get("income", 0)),
                "spent":  int(md.get("spent", 0)),
                "net":    int(md.get("net", 0)),
                "items":  items_agg,
            })
        return out

    try:
        import Restocker_db as db
    except Exception:
        db = None

    ids = set(markets.keys()) | {"main"}
    if db is not None:
        try:
            ids |= set(db.csn_all_market_ids())
        except Exception:
            pass

    result: dict = {}
    for mid in ids:
        rows = []
        if db is not None:
            try:
                rows = _parse_hist(db.csn_get_market(mid), mid)
            except Exception:
                rows = []
        if not rows and _YAML_AVAILABLE:          # legacy YAML fallback
            try:
                with open(_market_history_file(mid, markets.get(mid)), encoding="utf-8") as f:
                    rows = _parse_hist(_yaml.safe_load(f), mid)
            except FileNotFoundError:
                rows = []
            except Exception as e:
                print(f"[earnings] YAML fallback failed for '{mid}': {e}")
        if rows:
            result[mid] = rows

    result.setdefault("main", [])
    for _hid in _earnings_hidden_markets():
        result.pop(_hid, None)
    return result


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
        hist = db.get_csn_history()
        months = sorted(hist.values(), key=lambda x: x.get("month", ""))
        return [{
            "month":  m.get("month", ""),
            "label":  m.get("label", m.get("month", "")),
            "income": int(m.get("income", 0)),
            "spent":  int(m.get("spent", 0)),
            "net":    int(m.get("net", 0)),
        } for m in months]
    except Exception:
        pass
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
    items:[{item,sold,bought,net}]}]}]}. Months sorted oldest→newest.
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
                        "item":   item,
                        "sold":   int(iv.get("sold_qty", 0) or 0),
                        "bought": int(iv.get("bought_qty", 0) or 0),
                        "net":    int(round(float(iv.get("net_coins", 0) or 0))),
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
            rows   = db.get_price_history(mid, limit=60)
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
                for _it, _x in (_dbk.get_market_stock(_m["mid"]) or {}).items():
                    _px = _x.get("sell_price")
                    if _px is None:
                        _px = _x.get("buy_price")
                    if _px is not None:
                        _assets += float(_x.get("stock") or 0) * float(_px)
            except Exception:
                pass
        _fs = _fund * (_m["mcap"] / _tot_mcap)
        _mc = _m["mcap"] or 1.0
        _m["backing_pct"] = round(100.0 * (_m["treasury"] + _assets + _fs) / _mc, 1)
    index = None
    try:
        hist = db.get_market_index_history(200)
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
    return {"markets": out, "index": index}



_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Abexilas Economy Hub</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js">

</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --font-data:    'IBM Plex Mono', ui-monospace, "SFMono-Regular", Menlo, monospace;
    --font-ui:      'Space Grotesk', system-ui, sans-serif;
    --font-head:    var(--font-ui);
    --font-mono:    var(--font-data);
    --font-display: var(--font-ui);

    --bg:            #0A0A0A;
    --surface:       #111111;
    --panel2:        #161616;
    --overlay:       #1C1C1C;
    --border:        #1E1E1E;
    --border-dim:    #191919;
    --border-strong: #2A2A2A;

    --text:    #F0F0F0;
    --text-body: #BBBBBB;
    --muted:   #666666;
    --faint:   #444444;

    --green:   #22FF7A;
    --green-dim: #1A9E4F;
    --accent:  #22FF7A;
    --red:     #FF4444;
    --amber:   #F5A623;
    --gold:    #F5A623;
    --yellow:  #F5A623;
    --blue:    #4A9EFF;
    --purple:  #B47FFF;
    --shadow:  none;

    --market-bnl:    #4A9EFF;
    --market-nether: #FF6B35;
    --market-end:    #B47FFF;
    --market-sky:    #22FF7A;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { color-scheme: dark; }
  body {
    background-color: var(--bg);
    background-image: radial-gradient(var(--border-strong) 0.5px, transparent 0.5px);
    background-size: 26px 26px;
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 13px;
    line-height: 1.5;
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }
  /* DATA renders in the mono terminal face with tabular figures. */
  .mono, td, .stat-card .val, .badge, .coin-badge, .market-tag, .est-tag,
  .t-price, .t-chg, .updated, code, .own-price, .ownin, .search-wrap input,
  .item-name {
    font-family: var(--font-data);
    font-variant-numeric: tabular-nums slashed-zero;
    font-feature-settings: "tnum" 1, "zero" 1;
  }

  .market-dot {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    margin-right: 6px; vertical-align: middle; flex-shrink: 0;
  }

  /* ─── Header / topbar ─── */
  header {
    position: sticky; top: 0; z-index: 90; height: 50px;
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 0 20px; display: flex; align-items: center; justify-content: space-between; gap: 0;
  }
  .logo { display: flex; align-items: center; gap: 11px; text-decoration: none; }
  .logo-icon {
    width: 28px; height: 28px; background: var(--accent); border: none; border-radius: 0;
    display: flex; align-items: center; justify-content: center; color: #000;
  }
  .logo-icon svg { width: 15px; height: 15px; display: block; }
  .logo-text { font-family: var(--font-ui); font-size: 14px; font-weight: 600; color: var(--text); letter-spacing: .01em; }
  .logo-sub  { font-family: var(--font-data); font-size: 8.5px; color: var(--muted); margin-top: 2px; font-weight: 400; text-transform: uppercase; letter-spacing: .18em; }
  .header-right { display: flex; align-items: center; gap: 14px; }
  .auth-area { display: flex; align-items: center; gap: 8px; font-size: 12px; }
  .auth-btn {
    background: var(--accent); color: #000; border: none; border-radius: 0;
    padding: 6px 14px; font-family: var(--font-ui); font-size: 11px; font-weight: 600;
    cursor: pointer; text-transform: uppercase; letter-spacing: .06em; transition: opacity .15s;
  }
  .auth-btn:hover { opacity: .85; }
  .auth-btn:active { opacity: .7; }
  .auth-btn.ghost { background: transparent; border: 1px solid var(--border); color: var(--muted); }
  .auth-btn.ghost:hover { border-color: var(--border-strong); color: var(--text); opacity: 1; }
  .auth-name { font-family: var(--font-data); color: var(--accent); font-weight: 500; }
  .updated { font-family: var(--font-data); font-size: 9.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .1em; }

  /* ─── Nav ─── */
  nav {
    position: sticky; top: 50px; z-index: 80;
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 0 20px; display: flex; gap: 0;
  }
  .nav-tab {
    padding: 11px 16px; font-family: var(--font-ui); font-size: 11px; font-weight: 600;
    color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px;
    transition: color .12s, border-color .12s; user-select: none;
    display: flex; align-items: center; gap: 7px; text-transform: uppercase; letter-spacing: .11em;
  }
  .nav-tab svg { width: 14px; height: 14px; opacity: .7; }
  .nav-tab:hover { color: var(--text); }
  .nav-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .nav-tab.active svg { opacity: 1; }

  /* ─── Main ─── */
  main { max-width: 1240px; margin: 0 auto; padding: 24px 20px; }
  .page { display: none; }
  .page.active { display: block; }

  /* ─── Stat blocks: inline big number + small label (NOT cards) ─── */
  .stats {
    display: flex; flex-wrap: wrap; gap: 14px 40px; align-items: flex-end;
    background: none; border: none; border-bottom: 1px solid var(--border);
    padding: 6px 2px 20px; margin-bottom: 24px;
  }
  .stat-card { background: none; border: none; padding: 0; text-align: left; display: flex; flex-direction: column; gap: 5px; }
  .stat-card .val { font-family: var(--font-data); font-size: 30px; font-weight: 600; color: var(--text); letter-spacing: -.02em; line-height: 1; }
  .stat-card .lbl { font-family: var(--font-ui); font-size: 10px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .12em; }

  /* ─── Filters / inputs / tabs ─── */
  .filters { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }
  .search-wrap { flex: 1; min-width: 200px; position: relative; }
  .search-wrap input {
    width: 100%; background: var(--surface); border: 1px solid var(--border); border-radius: 2px;
    color: var(--text); padding: 8px 12px 8px 34px; font-size: 12.5px; outline: none; transition: border-color .12s;
  }
  .search-wrap input::placeholder { color: var(--faint); }
  .search-wrap input:focus { border-color: var(--border-strong); }
  .search-wrap .icon { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--muted); pointer-events: none; }
  .search-wrap .icon svg { width: 14px; height: 14px; display: block; }
  .market-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
  .tab, .market-tab {
    display: inline-flex; align-items: center; gap: 6px;
    background: transparent; border: 1px solid var(--border); border-radius: 0; color: var(--muted);
    padding: 6px 12px; cursor: pointer; font-family: var(--font-ui); font-size: 11px; font-weight: 500;
    transition: all .12s; user-select: none; text-transform: uppercase; letter-spacing: .06em;
  }
  .tab:hover, .market-tab:hover { border-color: var(--border-strong); color: var(--text); }
  .tab.active, .market-tab.active { border-color: var(--accent); color: var(--accent); background: rgba(34,255,122,.07); }

  /* ─── Tables (primary component) ─── */
  .table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: 0; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { width: 100%; border-collapse: collapse; }
  thead tr { background: transparent; }
  th {
    padding: 7px 12px; text-align: left; font-family: var(--font-ui); font-size: 10px; font-weight: 600;
    color: var(--muted); cursor: pointer; user-select: none; white-space: nowrap;
    border-bottom: 1px solid var(--border); text-transform: uppercase; letter-spacing: .1em;
  }
  th:hover { color: var(--text); }
  th .sort-arrow { margin-left: 4px; opacity: .4; }
  th.sorted { color: var(--accent); }
  th.sorted .sort-arrow { opacity: 1; color: var(--accent); }
  tbody tr { border-bottom: 1px solid var(--border-dim); transition: background .1s; }
  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: var(--panel2); }
  td { padding: 7px 12px; vertical-align: middle; font-size: 12px; color: var(--text); white-space: nowrap; }
  td svg { vertical-align: middle; }
  .item-name { font-weight: 500; color: var(--text); font-family: var(--font-data); }

  /* ─── Badges / values — monochrome base, semantic color only ─── */
  .badge { display: inline-block; padding: 0; font-weight: 500; font-size: 12px; background: none; border: none; }
  .coin-badge  { color: var(--text); font-weight: 500; }
  .stock-hi    { color: var(--green); }
  .stock-mid   { color: var(--amber); }
  .stock-lo    { color: var(--red); }
  .market-tag  {
    display: inline-flex; align-items: center; gap: 4px;
    font-family: var(--font-data); color: var(--muted); font-size: 9.5px; font-weight: 500;
    padding: 2px 8px; border: 1px solid var(--border-strong); border-radius: 999px;
    background: transparent; text-transform: uppercase; letter-spacing: .06em;
  }
  .status-open    { color: var(--green); }
  .status-claimed { color: var(--amber); }
  .status-done    { color: var(--blue); }
  .pos-badge   { color: var(--green); font-weight: 500; }
  .neg-badge   { color: var(--red); font-weight: 500; }
  .up { color: var(--green); }
  .down { color: var(--red); }

  /* progress */
  .prog-wrap { min-width: 100px; }
  .prog-track { background: var(--border-strong); border-radius: 0; height: 4px; margin-top: 4px; overflow: hidden; }
  .prog-fill { height: 100%; background: var(--accent); transition: width .3s; }
  .prog-label { font-family: var(--font-data); font-size: 10px; color: var(--muted); }

  /* ─── Panels / charts ─── */
  .chart-section, .chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: 0; padding: 16px; margin-bottom: 18px; }
  .chart-title { font-family: var(--font-ui); font-size: 10px; text-transform: uppercase; letter-spacing: .13em; color: var(--muted); font-weight: 600; margin-bottom: 14px; }
  .chart-box { position: relative; height: 300px; width: 100%; }
  .chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 760px) { .chart-grid { grid-template-columns: 1fr; } }

  /* stocks-page ticker cards (segmented, sharp) */
  .ticker { display: flex; gap: 1px; overflow-x: auto; padding: 0; margin-bottom: 18px; background: var(--border); border: 1px solid var(--border); scrollbar-width: thin; }
  .tick { flex: 0 0 auto; background: var(--surface); padding: 10px 16px; min-width: 155px; }
  .tick .t-name { font-family: var(--font-data); font-size: 9px; color: var(--muted); text-transform: uppercase; letter-spacing: .1em; }
  .tick .t-price { font-family: var(--font-data); font-size: 16px; font-weight: 600; margin-top: 5px; color: var(--text); }
  .tick .t-chg { font-family: var(--font-data); font-size: 12px; font-weight: 500; margin-top: 2px; }

  .est-tag { display: inline-block; margin-left: 6px; font-size: 8.5px; padding: 1px 5px; border-radius: 999px; background: transparent; color: var(--muted); border: 1px solid var(--border-strong); vertical-align: middle; font-family: var(--font-data); text-transform: uppercase; letter-spacing: .06em; }

  /* css bar fallback */
  .bar-chart { display: flex; align-items: flex-end; gap: 5px; height: 120px; }
  .bar-col { display: flex; flex-direction: column; align-items: center; flex: 1; min-width: 0; }
  .bar-pos { background: var(--green); width: 100%; min-height: 2px; }
  .bar-neg { background: var(--red); width: 100%; min-height: 2px; }
  .bar-lbl { font-family: var(--font-data); font-size: 8.5px; color: var(--muted); margin-top: 5px; text-align: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; width: 100%; }

  /* empty / section / footer / code */
  .empty { text-align: center; padding: 54px 0; color: var(--muted); font-family: var(--font-data); font-size: 12px; }
  .empty .big { margin-bottom: 12px; color: var(--faint); }
  .empty .big svg { width: 30px; height: 30px; }
  .section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .section-title { font-family: var(--font-ui); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .1em; }
  footer { margin-top: 44px; text-align: center; font-family: var(--font-data); font-size: 10px; color: var(--faint); padding-bottom: 24px; text-transform: uppercase; letter-spacing: .12em; }
  code { background: var(--panel2); border: 1px solid var(--border); border-radius: 0; padding: 1px 5px; font-family: var(--font-data); font-size: .92em; font-weight: 500; color: var(--accent); }
  .live-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--green); margin-right: 6px; vertical-align: middle; }

  /* owner panel */
  .mini-btn { background: transparent; border: 1px solid var(--border); color: var(--muted); border-radius: 0; padding: 5px 10px; font-family: var(--font-ui); font-size: 10px; font-weight: 600; cursor: pointer; transition: all .12s; text-transform: uppercase; letter-spacing: .05em; }
  .mini-btn:hover { border-color: var(--border-strong); color: var(--text); }
  .mini-btn.danger:hover { border-color: var(--red); color: var(--red); }
  .own-price, .ownin { background: var(--bg); border: 1px solid var(--border); border-radius: 2px; color: var(--text); padding: 7px 9px; font-size: 12.5px; outline: none; font-family: var(--font-data); }
  .own-price:focus, .ownin:focus { border-color: var(--border-strong); }
  .lblmini { font-family: var(--font-ui); font-size: 9px; text-transform: uppercase; letter-spacing: .11em; color: var(--muted); margin-bottom: 4px; font-weight: 600; }

  /* page-in stagger */
  @keyframes panel-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
  .page.active > * { animation: panel-in .3s ease both; }

  @media (max-width: 640px) {
    header { position: static; padding: 0 14px; height: auto; min-height: 50px; flex-wrap: wrap; }
    nav { position: static; padding: 0 8px; overflow-x: auto; }
    .nav-tab { padding: 10px 10px; font-size: 10px; white-space: nowrap; }
    main { padding: 16px 12px; }
    .stats { gap: 12px 24px; }
    .stat-card .val { font-size: 22px; }
    td, th { padding: 7px 9px; font-size: 11.5px; }
    .logo-sub { display: none; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; }
  }
</style>
</head>
<body>

<header>
  <a class="logo" href="/">
    <div class="logo-icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20V12M9 20V5M14 20V9M19 20V14"/></svg>
    </div>
    <div>
      <div class="logo-text">Abexilas Economy Hub</div>
      <div class="logo-sub">Live market data</div>
    </div>
  </a>
  <div class="header-right">
    <span id="auth-area" class="auth-area"></span>
    <span class="updated" id="updated-ts"></span>
  </div>
</header>

<nav>
  <div class="nav-tab active" data-page="inventory">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v4H3zM3 10h18v4H3zM3 17h18v4H3z"/></svg>Inventory
  </div>
  <div class="nav-tab" data-page="earnings">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 16v-5M12 16V8M17 16v-9"/></svg>Ledger
  </div>
  <div class="nav-tab" data-page="stocks">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>Exchange
  </div>
  <div class="nav-tab" data-page="orders">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>Orders
  </div>
  <div class="nav-tab" data-page="teams">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>Teams
  </div>
  <div class="nav-tab" data-page="mymarket" id="nav-mymarket" style="display:none">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z"/></svg>My Market
  </div>
</nav>

<main>

  <!-- ══════════════════════════ INVENTORY PAGE ══════════════════════════ -->
  <style>
  #page-inventory .iv-bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 16px}
  #page-inventory .iv-tabs{display:flex;gap:6px;flex-wrap:wrap}
  #page-inventory .iv-tab{padding:6px 12px;border:1px solid var(--border);color:var(--muted);font-size:12px;cursor:pointer;font-family:var(--font-data)}
  #page-inventory .iv-tab:hover{color:var(--text)}
  #page-inventory .iv-tab.active{border-color:var(--accent);color:var(--accent)}
  #page-inventory .iv-search{background:var(--panel2);border:1px solid var(--border);color:var(--text);padding:8px 11px;font-size:12px;font-family:var(--font-data);flex:1;min-width:200px}
  #page-inventory td.item-name{max-width:340px;white-space:normal;word-break:break-word;line-height:1.3}
  #page-inventory .iv-fill{height:8px;background:var(--panel2);position:relative;width:150px;display:inline-block;vertical-align:middle;overflow:hidden}
  #page-inventory .iv-fill>span{position:absolute;left:0;top:0;bottom:0}
  #page-inventory .iv-pos{color:var(--green)}#page-inventory .iv-amb{color:var(--amber)}#page-inventory .iv-neg{color:var(--red)}
  </style>
  <div class="page active" id="page-inventory">
    <div class="stats" id="stats-inventory"></div>
    <div class="iv-bar">
      <div class="iv-tabs" id="iv-markets"></div>
    </div>
    <div class="iv-bar">
      <input class="iv-search" id="iv-search" placeholder="Search items…" autocomplete="off">
      <button class="auth-btn" id="iv-genorders" style="display:none">⚡ Generate restock orders (→80%)</button>
      <span id="iv-genmsg" style="font-size:12px;color:var(--muted)"></span>
    </div>
    <div class="table-wrap">
      <table><thead><tr>
        <th data-ivsort="item">Item <span class="sort-arrow">↕</span></th>
        <th data-ivsort="pct" class="sorted">Fullness <span class="sort-arrow">↑</span></th>
        <th data-ivsort="stock">In stock <span class="sort-arrow">↕</span></th>
        <th data-ivsort="capacity">Capacity <span class="sort-arrow">↕</span></th>
        <th data-ivsort="price">Price ¢ <span class="sort-arrow">↕</span></th>
      </tr></thead><tbody id="iv-tbody"></tbody></table>
      <div class="empty" id="iv-empty" style="display:none">
        <div class="big"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3h18v4H3zM3 10h18v4H3zM3 17h18v4H3z"/></svg></div>No barrel scan yet — press the stock-scan key in-game and click your shops.
      </div>
    </div>
  </div>

  <!-- ══════════════════════════ PRICES PAGE ══════════════════════════ -->
  <div class="page" id="page-prices">
    <div class="stats" id="stats-prices"></div>
    <div class="filters">
      <div class="search-wrap">
        <span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg></span>
        <input type="text" id="search" placeholder="Search items…" autocomplete="off">
      </div>
      <div class="market-tabs" id="market-tabs"></div>
    </div>
    <div id="prices-owner-hint" style="display:none;margin:0 0 12px;padding:9px 14px;border:1px solid var(--accent);border-radius:8px;background:rgba(34,255,122,.07);color:var(--accent);font-size:13px"></div>
    <div class="chart-card" id="prices-chart-card" style="display:none">
      <div class="chart-title" id="prices-chart-title">Top items by sales volume</div>
      <div class="chart-box"><canvas id="prices-chart"></canvas></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th data-sort="name">Item <span class="sort-arrow">↕</span></th>
            <th data-sort="coin" class="sorted">Sell Price <span class="sort-arrow">↓</span></th>
            <th data-sort="stock">Stock / Full <span class="sort-arrow">↕</span></th>
            <th data-sort="sold">Sold (CSN) <span class="sort-arrow">↕</span></th>
            <th data-sort="market">Market <span class="sort-arrow">↕</span></th>
          </tr>
        </thead>
        <tbody id="prices-tbody"></tbody>
      </table>
      <div class="empty" id="prices-empty" style="display:none">
        <div class="big"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></svg></div>No items match your search.
      </div>
    </div>
  </div>

  <!-- ══════════════════════════ EARNINGS PAGE ══════════════════════════ -->
  <style>
  /* ── Ledger tab (v3 redesign) — lg- prefixed so nothing clashes ── */
  #page-earnings .lg-bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 16px}
  #page-earnings .lg-market-tabs{display:flex;gap:6px;flex-wrap:wrap}
  #page-earnings .lg-mtab{padding:6px 12px;border:1px solid var(--border);color:var(--muted);font-size:12px;cursor:pointer;font-family:var(--font-data)}
  #page-earnings .lg-mtab:hover{color:var(--text)}
  #page-earnings .lg-mtab.active{border-color:var(--accent);color:var(--accent)}
  #page-earnings .lg-select,#page-earnings .lg-search{background:var(--panel2);border:1px solid var(--border);color:var(--text);padding:8px 11px;font-size:12px;font-family:var(--font-data)}
  #page-earnings .lg-search{flex:1;min-width:200px}
  #page-earnings .lg-bento{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;margin-bottom:6px}
  #page-earnings .lg-tile{background:var(--surface);border:1px solid var(--border);padding:18px 20px;overflow:hidden}
  #page-earnings .lg-hero{grid-column:span 7}#page-earnings .lg-side{grid-column:span 5}
  #page-earnings .lg-sellers{grid-column:span 7}#page-earnings .lg-kpis{grid-column:span 5}
  @media(max-width:860px){#page-earnings .lg-hero,#page-earnings .lg-side,#page-earnings .lg-sellers,#page-earnings .lg-kpis{grid-column:span 12}}
  #page-earnings .lg-th{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:14px;display:flex;align-items:center;justify-content:space-between}
  #page-earnings .lg-mut{color:var(--muted);font-size:10px}
  #page-earnings .lg-hero-net{display:flex;align-items:baseline;gap:14px}
  #page-earnings .lg-big{font-family:var(--font-data);font-size:42px;font-weight:600;letter-spacing:-.02em;line-height:1}
  #page-earnings .lg-trend{font-family:var(--font-data);font-size:13px;padding:3px 9px}
  #page-earnings .lg-trend.up{color:var(--green);background:rgba(34,255,122,.1)}
  #page-earnings .lg-trend.down{color:var(--red);background:rgba(255,68,68,.1)}
  #page-earnings .lg-hero-sub{color:var(--muted);font-size:12px;margin:6px 0 12px}
  #page-earnings .lg-chartbox{position:relative;height:150px}
  #page-earnings .lg-donutbox{height:150px}
  #page-earnings .lg-donut-center{position:absolute;top:38%;left:0;right:0;text-align:center;pointer-events:none;padding:0 6px}
  #page-earnings .lg-dn{font-family:var(--font-data);font-size:17px;font-weight:600;line-height:1.15;white-space:nowrap}
  #page-earnings .lg-dl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
  #page-earnings .lg-legend{display:flex;flex-direction:column;gap:8px;margin-top:14px}
  #page-earnings .lg-lg{display:flex;align-items:center;justify-content:space-between;font-size:12px;color:var(--text-body);font-family:var(--font-data)}
  #page-earnings .lg-dot{width:9px;height:9px;border-radius:2px;display:inline-block;margin-right:8px}
  #page-earnings .lg-lead{display:flex;flex-direction:column;gap:1px}
  #page-earnings .lg-lrow{display:grid;grid-template-columns:20px 1fr 96px;align-items:center;gap:12px;padding:8px 2px;border-bottom:1px solid var(--border-dim,#181818)}
  #page-earnings .lg-lrow:last-child{border-bottom:none}
  #page-earnings .lg-lrank{font-family:var(--font-data);color:var(--faint);font-size:12px;text-align:center}
  #page-earnings .lg-lrow:first-child .lg-lrank{color:var(--amber)}
  #page-earnings .lg-lname{font-family:var(--font-data);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #page-earnings .lg-lbar{height:6px;background:var(--panel2);margin-top:6px;position:relative;overflow:hidden}
  #page-earnings .lg-lbar>span{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,var(--green),#17b558)}
  #page-earnings .lg-lbar.r>span{background:linear-gradient(90deg,#b53a3a,var(--red))}
  #page-earnings .lg-lmeta{text-align:right}
  #page-earnings .lg-lrev{font-family:var(--font-data);font-size:13px;font-weight:600}
  #page-earnings .lg-lqty{font-family:var(--font-data);font-size:10.5px;color:var(--muted)}
  #page-earnings .lg-kgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  #page-earnings .lg-kpi{background:var(--panel2);border:1px solid var(--border);padding:13px 14px}
  #page-earnings .lg-k{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}
  #page-earnings .lg-v{font-family:var(--font-data);font-size:20px;font-weight:600;margin-top:5px}
  #page-earnings .lg-kt{font-size:10px;color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #page-earnings .lg-pos{color:var(--green)}#page-earnings .lg-neg{color:var(--red)}#page-earnings .lg-amb{color:var(--amber)}#page-earnings .lg-muted{color:var(--muted)}
  #page-earnings .lg-section-h{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:26px 0 12px;display:flex;align-items:center;gap:10px}
  #page-earnings .lg-section-h::after{content:"";flex:1;height:1px;background:var(--border)}
  </style>
  <div class="page" id="page-earnings">
    <div class="lg-bar">
      <div class="lg-market-tabs" id="lg-markets"></div>
      <select class="lg-select" id="lg-month"></select>
    </div>
    <div class="lg-bento">
      <div class="lg-tile lg-hero">
        <div class="lg-th"><span id="lg-heroLbl">Net profit</span><span class="lg-mut">trend</span></div>
        <div class="lg-hero-net"><span class="lg-big lg-pos" id="lg-heroNet">—</span><span class="lg-trend up" id="lg-heroTrend" style="display:none"></span></div>
        <div class="lg-hero-sub" id="lg-heroSub"></div>
        <div class="lg-chartbox"><canvas id="lg-lineChart"></canvas></div>
      </div>
      <div class="lg-tile lg-side">
        <div class="lg-th"><span>Income vs spent</span></div>
        <div class="lg-chartbox lg-donutbox"><canvas id="lg-donutChart"></canvas>
          <div class="lg-donut-center"><div class="lg-dn lg-pos" id="lg-donutNet">—</div><div class="lg-dl">net margin <span id="lg-donutPct"></span></div></div>
        </div>
        <div class="lg-legend">
          <div class="lg-lg"><span><span class="lg-dot" style="background:var(--green)"></span>Income</span><span id="lg-lgInc">—</span></div>
          <div class="lg-lg"><span><span class="lg-dot" style="background:var(--red)"></span>Spent</span><span id="lg-lgExp">—</span></div>
        </div>
      </div>
      <div class="lg-tile lg-sellers">
        <div class="lg-th"><span id="lg-leadLbl">What's selling</span><span class="lg-mut">by revenue</span></div>
        <div class="lg-lead" id="lg-lead"></div>
      </div>
      <div class="lg-tile lg-kpis">
        <div class="lg-th"><span>At a glance</span></div>
        <div class="lg-kgrid">
          <div class="lg-kpi"><div class="lg-k">Items sold</div><div class="lg-v" id="lg-kSold">—</div><div class="lg-kt">units</div></div>
          <div class="lg-kpi"><div class="lg-k">Unique items</div><div class="lg-v" id="lg-kUniq">—</div><div class="lg-kt">distinct SKUs</div></div>
          <div class="lg-kpi"><div class="lg-k">Top earner</div><div class="lg-v lg-pos" id="lg-kTop">—</div><div class="lg-kt" id="lg-kTopN">—</div></div>
          <div class="lg-kpi"><div class="lg-k">Biggest cost</div><div class="lg-v lg-neg" id="lg-kCost">—</div><div class="lg-kt" id="lg-kCostN">none</div></div>
        </div>
      </div>
    </div>
    <div class="lg-section-h">Full ledger</div>
    <div class="lg-bar">
      <input class="lg-search" id="lg-q" placeholder="Search items…" autocomplete="off">
      <select class="lg-select" id="lg-flt"><option value="all">All items</option><option value="income">Income only</option><option value="expense">Expense only</option></select>
    </div>
    <div class="table-wrap">
      <table><thead><tr>
        <th data-lgsort="item">Item <span class="sort-arrow">↕</span></th>
        <th data-lgsort="sold">Sold <span class="sort-arrow">↕</span></th>
        <th data-lgsort="bought">Bought <span class="sort-arrow">↕</span></th>
        <th data-lgsort="net" class="sorted">Net ¢ <span class="sort-arrow">↓</span></th>
      </tr></thead><tbody id="lg-tbody"></tbody></table>
      <div class="empty" id="lg-empty" style="display:none">
        <div class="big"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 16v-5M12 16V8M17 16v-9"/></svg></div>No earnings recorded yet — run <code>/csn</code> in Discord to log a month.
      </div>
    </div>
  </div>

  <!-- ══════════════════════════ STOCKS PAGE ══════════════════════════ -->
  <div class="page" id="page-stocks">
    <div class="chart-card" id="index-card" style="display:none">
      <div class="chart-title">Abexilas Market Index</div>
      <div style="display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;margin-bottom:12px">
        <span class="val mono" id="index-value" style="font-size:34px;font-weight:600;color:var(--text)"></span>
        <span id="index-change" class="t-chg"></span>
        <span style="font-family:var(--font-data);font-size:11px;color:var(--muted)"><span id="index-mcap"></span> total cap &middot; <span id="index-markets"></span> markets</span>
      </div>
      <div class="chart-box" style="height:220px"><canvas id="index-chart"></canvas></div>
    </div>
    <div class="ticker" id="stock-ticker"></div>
    <div class="stats" id="stats-stocks"></div>
    <div id="my-holdings-card" class="chart-card" style="display:none">
      <div class="chart-title">Your holdings <span id="my-holdings-sub" style="color:var(--muted);font-weight:400"></span></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Market</th><th>Shares</th><th>Value</th><th>Cost basis</th><th>P/L</th></tr></thead>
          <tbody id="my-holdings-tbody"></tbody>
        </table>
      </div>
    </div>
    <div class="filters">
      <div class="market-tabs" id="stock-market-tabs"></div>
    </div>
    <div class="chart-card">
      <div class="chart-title" id="stock-chart-title">Share price history</div>
      <div class="chart-box"><canvas id="stock-chart"></canvas></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Market</th><th>Price</th><th>Change</th><th>Trend</th>
            <th>Market Cap</th><th>P/E</th><th>Div</th><th>Treasury</th><th>Backed</th><th>Holders</th>
          </tr>
        </thead>
        <tbody id="stocks-tbody"></tbody>
      </table>
      <div class="empty" id="stocks-empty" style="display:none">
        <div class="big"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg></div>No public markets yet — run <code>/market go_public</code> in Discord.
      </div>
    </div>
    <div id="holders-section" style="margin-top:28px;display:none">
      <div class="chart-title" style="margin-bottom:12px">Top holders · <span id="holders-market-name"></span>
        <a id="captable-link" href="#" target="_blank" style="float:right;font-size:13px;color:var(--accent);text-decoration:none">📊 Full cap table →</a></div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Holder</th><th>Shares</th><th>Value</th></tr></thead>
          <tbody id="holders-tbody"></tbody>
        </table>
      </div>
    </div>
    <div id="dividends-section" style="margin-top:28px;display:none">
      <div class="chart-title" style="margin-bottom:12px">Dividends &amp; Treasury</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Market</th><th>Div rate</th><th>Last payout</th><th>Per share</th><th>Treasury</th><th>Open orders</th></tr></thead>
          <tbody id="dividends-tbody"></tbody>
        </table>
      </div>
      <div style="font-size:11.5px;color:var(--faint);margin-top:8px">Dividends pay to shareholders pro-rata on each monthly CSN report. Treasury funds share buy-backs.</div>
    </div>
  </div>

  <!-- ══════════════════════════ ORDERS PAGE ══════════════════════════ -->
  <style>
  #page-orders .or-bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 16px}
  #page-orders .or-tabs{display:flex;gap:6px;flex-wrap:wrap}
  #page-orders .or-tab{padding:6px 12px;border:1px solid var(--border);color:var(--muted);font-size:12px;cursor:pointer;font-family:var(--font-data)}
  #page-orders .or-tab:hover{color:var(--text)}
  #page-orders .or-tab.active{border-color:var(--accent);color:var(--accent)}
  #page-orders .or-fill{height:7px;background:var(--panel2);position:relative;width:140px;display:inline-block;vertical-align:middle;overflow:hidden}
  #page-orders .or-fill>span{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,var(--green),#17b558)}
  #page-orders .or-tag{padding:2px 8px;font-size:11px;font-family:var(--font-data);border:1px solid var(--border)}
  #page-orders .or-open{color:var(--green);border-color:rgba(34,255,122,.3)}
  #page-orders .or-partial{color:var(--amber);border-color:rgba(245,166,35,.3)}
  #page-orders .or-claimed{color:var(--muted)}
  </style>
  <div class="page" id="page-orders">
    <div id="or-place" class="chart-card" style="margin-bottom:16px">
      <div class="chart-title">🛒 Place an order</div>
      <div id="or-place-locked" style="font-size:12.5px;color:var(--muted)">Log in (👤 top-right) to order — link your account with <code>/website_login</code> in Discord.</div>
      <div id="or-place-form" style="display:none">
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:10px">
          <div><div class="lblmini">Item</div><input id="or-item" list="or-catalog" class="ownin" placeholder="Search catalog…" style="width:240px" autocomplete="off"><datalist id="or-catalog"></datalist></div>
          <div><div class="lblmini">Qty</div><input id="or-qty" class="ownin" type="number" min="1" value="1" style="width:90px"></div>
          <button class="auth-btn" id="or-add" type="button">Add</button>
          <span id="or-add-msg" style="font-size:12px;color:var(--muted)"></span>
        </div>
        <table id="or-cart-tbl" style="display:none;width:100%;max-width:520px;margin-bottom:10px"><thead><tr><th>Item</th><th>Qty</th><th>Est. ¢</th><th></th></tr></thead><tbody id="or-cart"></tbody></table>
        <div><div class="lblmini">Notes (optional)</div><input id="or-notes" class="ownin" placeholder="e.g. for war, deliver to spawn" style="width:100%;max-width:520px"></div>
        <div style="margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <button class="auth-btn" id="or-submit" type="button">Submit order</button>
          <span id="or-submit-msg" style="font-size:12px;color:var(--muted)"></span>
        </div>
      </div>
    </div>
    <div class="stats" id="stats-orders"></div>
    <div class="or-bar"><div class="or-tabs" id="or-markets"></div></div>
    <div class="table-wrap">
      <table><thead><tr>
        <th>#</th><th>Item</th><th>Requested</th><th>Claimed</th><th>Progress</th><th>Status</th>
      </tr></thead><tbody id="or-tbody"></tbody></table>
      <div class="empty" id="or-empty" style="display:none">
        <div class="big"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></div>No open orders — all caught up, or none created yet.
      </div>
    </div>
  </div>

  <!-- ══════════════════════════ TEAMS PAGE ══════════════════════════ -->
  <div class="page" id="page-teams">
    <div class="stats" id="stats-teams"></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Team</th><th>Members</th><th>Orders</th><th>Sales</th><th>Futures</th><th>Total</th></tr></thead>
        <tbody id="teams-tbody"></tbody>
      </table>
      <div class="empty" id="teams-empty" style="display:none">
        <div class="big"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg></div>No team activity yet — managers earn as their workers fulfill orders &amp; sell.
      </div>
    </div>
    <div style="font-size:11.5px;color:var(--faint);margin-top:8px">Ranked by total coins (order payouts + chest-shop sales) over the last <span id="teams-window">7</span> days. In-game names only.</div>
  </div>

  <!-- ══════════════════════════ MY MARKET (owner-only) ══════════════════════════ -->
  <div class="page" id="page-mymarket">
    <div class="filters"><div class="market-tabs" id="owner-market-tabs"></div></div>
    <div class="stats" id="owner-stats"></div>
    <div class="chart-card">
      <div class="chart-title">Restock rewards <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— extra pay for workers who fill THIS market's orders</span></div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">
        <div><div class="lblmini">Loyalty × (points)</div><input id="loy-mult" class="ownin" type="number" step="0.1" min="0.1" placeholder="1.5" style="width:110px"></div>
        <div><div class="lblmini">Coin bonus / order</div><input id="loy-bonus" class="ownin" type="number" min="0" placeholder="500" style="width:130px"></div>
        <div><div class="lblmini">% bonus / order</div><input id="loy-pct" class="ownin" type="number" min="0" step="1" placeholder="20" style="width:110px" title="Extra pay as a % of the order's value — scales with order size"></div>
        <button class="auth-btn" id="loy-save">Save rewards</button>
        <span id="loy-msg" style="font-size:12px;color:var(--muted)"></span>
      </div>
      <div style="font-size:11.5px;color:var(--faint);margin-top:8px">Applies when a manager approves an order tagged to this market. 1× = normal, no bonus. Same setting as <code>/market loyalty</code> in Discord — each market is independent.</div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Order builder <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— tick what you restock, tune each item's target %, then build the order</span></div>
      <div id="ob-cats"></div>
      <div class="empty" id="ob-empty" style="display:none">No stock scan on file for this market yet.</div>
      <div style="display:flex;gap:12px;align-items:center;margin-top:12px">
        <button class="auth-btn" id="ob-build">Build order</button>
        <span id="ob-msg" style="font-size:12px;color:var(--muted)"></span>
      </div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Request futures <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— custom crafts made to order; a manager approves &amp; queues them for workers</span></div>
      <textarea id="fut-items" class="ownin" rows="4" placeholder="One item per line, e.g.&#10;2 barrels Warlord Potion (Str 2 + Speed 2)&#10;Sword Sharp V Fire Aspect II x10" style="width:100%;resize:vertical;font-family:var(--font-data)"></textarea>
      <input id="fut-notes" class="ownin" placeholder="Notes (optional)" style="width:100%;margin-top:8px">
      <div style="display:flex;gap:12px;align-items:center;margin-top:10px">
        <button class="auth-btn" id="fut-send">Submit futures request</button>
        <span id="fut-msg" style="font-size:12px;color:var(--muted)"></span>
      </div>
    </div>
    <div class="chart-card">
      <div class="chart-title">Log manual restock <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— stock you added by hand (bought via /pay)</span></div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">
        <div><div class="lblmini">Item</div><input id="rs-item" class="ownin" placeholder="Item name" list="owner-itemlist" style="width:200px"></div>
        <div><div class="lblmini">Qty</div><input id="rs-qty" class="ownin" type="number" min="1" placeholder="64" style="width:90px"></div>
        <div><div class="lblmini">Total cost</div><input id="rs-cost" class="ownin" type="number" min="0" placeholder="320" style="width:120px"></div>
        <button class="auth-btn" id="rs-add">Add stock</button>
        <span id="rs-msg" style="font-size:12px;color:var(--muted)"></span>
      </div>
      <datalist id="owner-itemlist"></datalist>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Item</th><th>Stock</th><th>Your price</th><th>Sold</th><th>Optimal</th><th>Actions</th></tr></thead>
        <tbody id="owner-tbody"></tbody>
      </table>
      <div class="empty" id="owner-empty" style="display:none">No items yet — log a restock or sell on the server to populate this.</div>
    </div>
    <div style="font-size:11.5px;color:var(--faint);margin-top:10px">Optimal blends your realized sell price, your cost, and the general market average across all shops. "Remove" does a full remove — it also adjusts historical net and your share price.</div>
  </div>

  </main>

<footer>Abexilas Economy Hub · Restocker</footer>

<script>
const ITEMS         = __ITEMS_JSON__;
const MARKETS       = __MARKETS_JSON__;
const EARNINGS      = __EARNINGS_JSON__;
const ALL_EARNINGS  = __ALL_EARNINGS_JSON__;
const MARKET_PRICES = __MARKET_PRICES_JSON__;
const STOCKS        = __STOCKS_JSON__;
const TEAMS         = __TEAMS_JSON__;

// ── Owner edit state (shared): which markets you own + CSRF, fetched once. Powers
// inline price/stock editing on the main table and the "you own this market" hint.
window.OWNER = { owned: [], csrf: "", ready: false };
window.ownsMarket = (mid) => window.OWNER.owned.includes(String(mid));
window.ownerSave = async (mid, item, patch) => {
  try {
    const r = await fetch("/api/owner/set_item", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": window.OWNER.csrf },
      body: JSON.stringify(Object.assign({ market_id: mid, item: item }, patch)),
    });
    return await r.json();
  } catch (e) { return { ok: false, error: "network" }; }
};
window.OWNER_READY = fetch("/api/me").then(r => r.json()).then(me => {
  window.OWNER.owned = ((me && me.owned) || []).map(o => String(o.mid));
  window.OWNER.csrf  = (me && me.csrf) || "";
  window.OWNER.ready = true;
  return window.OWNER;
}).catch(() => window.OWNER);
const INVENTORY     = __INVENTORY_JSON__;
const ORDERS        = __ORDERS_JSON__;
const UPDATED       = "__UPDATED__";

document.getElementById("updated-ts").textContent = "Updated: " + UPDATED;

// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function coin(n) { return Number(n).toLocaleString() + " ¢"; }
function num(n)  { return Number(n).toLocaleString(); }
const FACTION_COLORS = ["#4A9EFF","#FF6B35","#B47FFF","#22FF7A","#F5A623","#4A9EFF","#FF4444","#e0a83b"];
function factionColor(key) {
  key = String(key == null ? "" : key);
  let h = 0; for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return FACTION_COLORS[h % FACTION_COLORS.length];
}
function mktDot(key) { return `<span class="market-dot" style="background:${factionColor(key)}"></span>`; }
function _barGrad(c, rgb) {
  const ch = c.chart, area = ch.chartArea;
  if (!area) return `rgba(${rgb},.85)`;
  const g = ch.ctx.createLinearGradient(area.left, 0, area.right, 0);
  g.addColorStop(0, `rgba(${rgb},.45)`);
  g.addColorStop(1, `rgba(${rgb},.98)`);
  return g;
}
if (window.Chart) {
  Chart.defaults.font.family = "'IBM Plex Mono', ui-monospace, monospace";
  Chart.defaults.font.size = 10;
  Chart.defaults.color = "#666666";
}

// ── Nav tabs ───────────────────────────────────────────────────────────────
document.querySelectorAll(".nav-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".nav-tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById("page-" + tab.dataset.page).classList.add("active");
  });
});

// ══════════════════════════ PRICES ══════════════════════════════════════════
(function initPrices() {
  function mktName(mid) {
    if (!mid || mid === "main") return MARKETS["main"] ? MARKETS["main"].name : "Main";
    return MARKETS[mid] ? MARKETS[mid].name : mid;
  }

  // Merge curated catalog (items table) with prices derived from CSN history.
  // Seed every market with its CSN-derived prices (this is what surfaces BNL and
  // other non-main markets), then overlay the manually-curated items table so
  // curated prices/stock win where they exist for that same market+item.
  const byMarket = {};   // mid -> { name -> row }
  function slot(mid) { return (byMarket[mid] = byMarket[mid] || {}); }

  Object.entries(MARKET_PRICES).forEach(([mid, items]) => {
    const s = slot(mid);
    Object.entries(items).forEach(([name, v]) => {
      s[name] = { name, coin: v.coin || 0, stock: v.stock || 0, sold: v.sold || 0,
                  market: mid, est: true };
    });
  });
  Object.entries(ITEMS).forEach(([name, i]) => {
    const mid = i.market_id || "main";
    const s = slot(mid);
    const prev = s[name];
    s[name] = { name, coin: i.coin || 0, stock: i.stock || (prev ? prev.stock : 0) || 0,
                sold: prev ? prev.sold : 0, market: mid, est: false };
  });

  const allRows = [];
  Object.values(byMarket).forEach(s => Object.values(s).forEach(r => allRows.push(r)));

  // Stats
  const bar = document.getElementById("stats-prices");
  const total   = allRows.length;
  const inStock = allRows.filter(i => i.stock > 0).length;
  const avg     = total ? Math.round(allRows.reduce((s,i) => s+i.coin, 0) / total) : 0;
  const marketSet = new Set(allRows.map(i => i.market));
  const mkts    = (Object.keys(MARKETS).length || marketSet.size) || 1;
  [
    [total,       "Total Items"],
    [inStock,     "In Stock"],
    [avg + " ¢",  "Avg Price"],
    [mkts,        "Markets"],
  ].forEach(([val, lbl]) => {
    const d = document.createElement("div");
    d.className = "stat-card";
    d.innerHTML = `<div class="val">${val}</div><div class="lbl">${lbl}</div>`;
    bar.appendChild(d);
  });

  // Market tabs
  let activeMarket = "all";
  const tabsEl = document.getElementById("market-tabs");
  function addMTab(id, label) {
    const btn = document.createElement("button");
    btn.className = "tab" + (id === "all" ? " active" : "");
    if (id === "all") btn.textContent = label;
    else btn.innerHTML = mktDot(id) + esc(label);
    btn.addEventListener("click", () => {
      activeMarket = id;
      tabsEl.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
      btn.classList.add("active");
      render();
    });
    tabsEl.appendChild(btn);
  }
  addMTab("all", "All");
  // Union of registered markets and any market that has price rows.
  const allMarketIds = [...new Set([...Object.keys(MARKETS), ...marketSet])];
  allMarketIds.forEach(mid => addMTab(mid, mktName(mid)));

  // Table
  let sortCol = "coin", sortAsc = false;
  const tbody  = document.getElementById("prices-tbody");
  const invLookup = {};
  ((typeof INVENTORY !== "undefined" && INVENTORY && INVENTORY.markets) || []).forEach(m => {
    const mm = invLookup[m.market_id] = {};
    (m.items || []).forEach(x => { mm[x.item] = x; });
  });
  function stockCell(r) {
    const inv = invLookup[r.market] && invLookup[r.market][r.name];
    if (inv) {
      const cap = inv.capacity || inv.stock || 1;
      const pct = inv.capacity > 0 ? (100 * inv.stock / cap) : 100;
      const col = pct <= 20 ? "var(--down)" : (pct <= 50 ? "#E8B339" : "var(--accent)");
      return `<span class="badge" style="color:${col}">${num(inv.stock)}</span>`
           + `<span style="color:var(--muted);font-size:10px"> /${num(cap)} · ${pct.toFixed(0)}%</span>`;
    }
    return `<span class="badge ${stockCls(r.stock)}">${num(r.stock)}</span>`;
  }
  const emptyEl = document.getElementById("prices-empty");
  const searchEl = document.getElementById("search");

  function stockCls(s) {
    return s > 32 ? "stock-hi" : s > 0 ? "stock-mid" : "stock-lo";
  }

  let priceChart = null;
  function renderChart(rows) {
    const card = document.getElementById("prices-chart-card");
    const top = rows.filter(r => r.sold > 0).sort((a,b) => b.sold - a.sold).slice(0, 10);
    if (!top.length) { card.style.display = "none"; if (priceChart) { priceChart.destroy(); priceChart = null; } return; }
    card.style.display = "";
    const ctx = document.getElementById("prices-chart");
    if (priceChart) priceChart.destroy();
    priceChart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: top.map(r => r.name.length > 26 ? r.name.slice(0,25)+"…" : r.name),
        datasets: [{
          label: "Units sold (CSN)",
          data: top.map(r => r.sold),
          backgroundColor: (c) => _barGrad(c, "34,255,122"),
          borderColor: "#22FF7A",
          borderWidth: 0,
          borderRadius: 0,
          maxBarThickness: 15,
        }],
      },
      options: {
        indexAxis: "y",
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: "#666666" }, grid: { color: "rgba(255,255,255,.05)" }, border: { display: false } },
          y: { ticks: { color: "#BBBBBB" }, grid: { display: false }, border: { color: "#1E1E1E" } },
        },
      },
    });
  }

  function render() {
    const q = searchEl.value.trim().toLowerCase();
    let rows = (activeMarket === "all")
      ? allRows.slice()
      : Object.values(byMarket[activeMarket] || {});
    if (q) rows = rows.filter(r => r.name.toLowerCase().includes(q));
    rows.sort((a, b) => {
      let va = a[sortCol], vb = b[sortCol];
      if (typeof va === "string") { va = va.toLowerCase(); vb = vb.toLowerCase(); }
      return sortAsc ? (va < vb ? -1 : va > vb ? 1 : 0) : (va > vb ? -1 : va < vb ? 1 : 0);
    });
    tbody.innerHTML = "";
    emptyEl.style.display = rows.length ? "none" : "";
    // Owner hint + inline editing when the logged-in user owns the active market.
    const ownActive = (activeMarket !== "all") && window.ownsMarket && window.ownsMarket(activeMarket);
    const hintEl = document.getElementById("prices-owner-hint");
    if (hintEl) {
      hintEl.style.display = ownActive ? "" : "none";
      if (ownActive) hintEl.textContent = "✏️ You own this market — edit price or stock right here and press Enter to save (set stock to 0 to zero it, or use My Market to remove an item).";
    }
    rows.forEach(r => {
      const tr = document.createElement("tr");
      const estTag = r.est ? `<span class="est-tag" title="Estimated from CSN sales">est</span>` : "";
      const own = !!(window.ownsMarket && window.ownsMarket(r.market));
      tr.innerHTML = `
        <td class="item-name">${esc(r.name)}${estTag}</td>
        <td class="c-price"></td>
        <td class="c-stock"></td>
        <td><span class="badge" style="color:var(--muted)">${num(r.sold)}</span></td>
        <td><span class="badge market-tag">${mktDot(r.market)}${esc(mktName(r.market))}</span></td>`;
      const cp = tr.querySelector(".c-price"), cs = tr.querySelector(".c-stock");
      if (own) {
        try { cp.appendChild(ownerCell(r, "coin", Math.round((r.coin || 0) * 100) / 100)); }
        catch (e) { cp.innerHTML = `<span class="badge coin-badge">${num(r.coin)} ¢</span>`; }
        try { cs.appendChild(ownerCell(r, "stock", Math.round(r.stock || 0))); }
        catch (e) { cs.innerHTML = stockCell(r); }
      } else {
        cp.innerHTML = `<span class="badge coin-badge">${num(r.coin)} ¢</span>`;
        cs.innerHTML = stockCell(r);
      }
      tbody.appendChild(tr);
    });
    renderChart(rows);
  }

  function ownerCell(r, field, val) {
    const inp = document.createElement("input");
    inp.type = "number"; if (field === "coin") inp.step = "any"; inp.value = val; inp.style.width = "78px";
    inp.style.background = "var(--panel2)"; inp.style.border = "1px solid var(--border-strong)";
    inp.style.color = "var(--text)"; inp.style.borderRadius = "6px"; inp.style.padding = "3px 6px";
    inp.title = "You own this market — edit and press Enter to save";
    const save = async () => {
      inp.style.opacity = ".5";
      const patch = {}; patch[field] = Number(inp.value);
      const res = await window.ownerSave(r.market, r.name, patch);
      inp.style.opacity = "1";
      inp.style.borderColor = (res && res.ok) ? "var(--accent)" : "#f85149";
      setTimeout(() => { inp.style.borderColor = "var(--border-strong)"; }, 1000);
      if (res && res.ok) { r[field] = Number(inp.value); }
    };
    inp.addEventListener("change", save);
    inp.addEventListener("keydown", e => { if (e.key === "Enter") inp.blur(); });
    return inp;
  }

  // Once we know which markets the user owns, re-render so edit fields appear.
  if (window.OWNER_READY) window.OWNER_READY.then(function () { try { render(); } catch (e) {} });

  document.querySelectorAll("#page-prices th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (sortCol === col) sortAsc = !sortAsc;
      else { sortCol = col; sortAsc = col === "name"; }
      document.querySelectorAll("#page-prices th").forEach(t => {
        t.classList.remove("sorted");
        const a = t.querySelector(".sort-arrow"); if (a) a.textContent = "↕";
      });
      th.classList.add("sorted");
      const a = th.querySelector(".sort-arrow"); if (a) a.textContent = sortAsc ? "↑" : "↓";
      render();
    });
  });
  searchEl.addEventListener("input", render);
  render();
})();

// ══════════════════════════ EARNINGS ════════════════════════════════════════
(function initEarnings() {
  return;  // ── legacy Earnings renderer replaced by initLedger() below (v3 redesign) ──
  // Build market tab list from ALL_EARNINGS keys that have data
  const marketIds = Object.keys(ALL_EARNINGS).filter(mid => ALL_EARNINGS[mid] && ALL_EARNINGS[mid].length > 0);
  let activeMarket = marketIds.includes("main") ? "main" : (marketIds[0] || "main");

  const tabsEl  = document.getElementById("earnings-market-tabs");
  const statsEl = document.getElementById("stats-earnings");
  const chartEl = document.getElementById("earnings-chart");
  const itemsChartEl = document.getElementById("earnings-items-chart");
  const tbody   = document.getElementById("earnings-tbody");
  const emptyEl = document.getElementById("earnings-empty");
  let netChart = null, itemsChart = null;

  function marketLabel(mid) {
    const m = MARKETS[mid];
    return m ? (m.name || mid) : (mid === "main" ? "Main Market" : mid);
  }

  // Build market selector tabs
  if (marketIds.length > 1) {
    marketIds.forEach(mid => {
      const btn = document.createElement("div");
      btn.className = "market-tab" + (mid === activeMarket ? " active" : "");
      btn.textContent = marketLabel(mid);
      btn.addEventListener("click", () => {
        activeMarket = mid;
        tabsEl.querySelectorAll(".market-tab").forEach(t => t.classList.remove("active"));
        btn.classList.add("active");
        render(mid);
      });
      tabsEl.appendChild(btn);
    });
  }

  function render(mid) {
    const data = ALL_EARNINGS[mid] || [];

    // Stats
    statsEl.innerHTML = "";
    const totalNet    = data.reduce((s, m) => s + m.net, 0);
    const totalIncome = data.reduce((s, m) => s + m.income, 0);
    const avgNet      = data.length ? Math.round(totalNet / data.length) : 0;
    [
      [data.length,                                                         "Months Tracked"],
      [num(Math.round(totalIncome)) + " ¢",                                "Total Income"],
      [(totalNet >= 0 ? "+" : "") + num(Math.round(totalNet)) + " ¢",     "Total Net"],
      [(avgNet >= 0 ? "+" : "") + num(avgNet) + " ¢",                     "Avg Net/Mo"],
    ].forEach(([val, lbl], i) => {
      const d = document.createElement("div");
      d.className = "stat-card";
      const color = (i >= 2 && (i === 2 ? totalNet : avgNet) < 0) ? "var(--red)" : (i >= 2 ? "var(--green)" : "var(--blue)");
      d.innerHTML = `<div class="val" style="color:${color}">${val}</div><div class="lbl">${lbl}</div>`;
      statsEl.appendChild(d);
    });

    // ── Income / Spent / Net combo chart (last 12 months) ─────────────────────
    const recent = data.slice(-12);
    const shortLbl = m => m.label.replace(/^(\\w{3})\\w+ /, '$1 ');
    if (netChart) { netChart.destroy(); netChart = null; }
    if (recent.length) {
      netChart = new Chart(chartEl, {
        data: {
          labels: recent.map(shortLbl),
          datasets: [
            { type: "bar", label: "Income", data: recent.map(m => m.income),
              backgroundColor: "rgba(34,255,122,.9)", borderColor: "#22FF7A", borderWidth: 0, borderRadius: 0, maxBarThickness: 22, order: 2 },
            { type: "bar", label: "Spent", data: recent.map(m => m.spent),
              backgroundColor: "rgba(255,68,68,.45)", borderColor: "#FF4444", borderWidth: 0, borderRadius: 0, maxBarThickness: 22, order: 2 },
            { type: "line", label: "Net", data: recent.map(m => m.net),
              borderColor: "#4A9EFF", backgroundColor: "rgba(74,158,255,.08)", fill: true, tension: 0.35,
              borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 3, pointBackgroundColor: "#4A9EFF", order: 1 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { labels: { color: "#BBBBBB", boxWidth: 10, font: { family: "IBM Plex Mono", size: 10 } } },
            tooltip: { callbacks: { label: c => `${c.dataset.label}: ${Number(c.parsed.y).toLocaleString()} ¢` } },
          },
          scales: {
            x: { ticks: { color: "#666666" }, grid: { display: false }, border: { color: "#1E1E1E" } },
            y: { ticks: { color: "#666666", callback: v => Number(v).toLocaleString() }, grid: { color: "rgba(255,255,255,.05)" }, border: { display: false } },
          },
        },
      });
    }

    // ── Top 10 items by units sold ────────────────────────────────────────────
    const itemAgg = {};
    data.forEach(m => {
      if (!m.items) return;
      Object.entries(m.items).forEach(([n, iv]) => {
        itemAgg[n] = (itemAgg[n] || 0) + (iv.sold || 0);
      });
    });
    const topItems = Object.entries(itemAgg).filter(([,s]) => s > 0)
      .sort((a,b) => b[1] - a[1]).slice(0, 10);
    if (itemsChart) { itemsChart.destroy(); itemsChart = null; }
    if (topItems.length) {
      itemsChart = new Chart(itemsChartEl, {
        type: "bar",
        data: {
          labels: topItems.map(([n]) => n.length > 26 ? n.slice(0,25)+"…" : n),
          datasets: [{ label: "Units sold", data: topItems.map(([,s]) => s),
            backgroundColor: (c) => _barGrad(c, "74,158,255"), borderColor: "#4A9EFF", borderWidth: 0, borderRadius: 0, maxBarThickness: 15 }],
        },
        options: {
          indexAxis: "y",
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: "#666666" }, grid: { color: "rgba(255,255,255,.05)" }, border: { display: false } },
            y: { ticks: { color: "#BBBBBB" }, grid: { display: false }, border: { color: "#1E1E1E" } },
          },
        },
      });
    }

    // Monthly table
    tbody.innerHTML = "";
    if (!data.length) { emptyEl.style.display = ""; return; }
    emptyEl.style.display = "none";
    data.forEach(m => {
      const tr = document.createElement("tr");
      const netCls = m.net >= 0 ? "pos-badge" : "neg-badge";
      const netStr = (m.net >= 0 ? "+" : "") + num(m.net) + " ¢";
      tr.innerHTML = `
        <td class="item-name">${esc(m.label)}</td>
        <td><span class="badge coin-badge">${num(m.income)} ¢</span></td>
        <td><span class="badge stock-lo">${num(m.spent)} ¢</span></td>
        <td><span class="badge ${netCls}">${netStr}</span></td>`;
      tbody.appendChild(tr);
    });

    // ── Item breakdown ──────────────────────────────────────────────────────
    // Aggregate sold/bought per item across all months for this market
    const itemTotals = {};
    data.forEach(m => {
      if (!m.items) return;
      Object.entries(m.items).forEach(([iname, iv]) => {
        if (!itemTotals[iname]) itemTotals[iname] = {sold: 0, bought: 0};
        itemTotals[iname].sold   += iv.sold  || 0;
        itemTotals[iname].bought += iv.bought || 0;
      });
    });

    const itemRows = Object.entries(itemTotals).map(([name, v]) => ({
      name,
      sold:    v.sold,
      bought:  v.bought,
      missing: v.sold - v.bought,   // positive = short (need more), negative = surplus
    }));

    const section  = document.getElementById("item-stats-section");
    const itemTbody = document.getElementById("items-breakdown-tbody");
    itemTbody.innerHTML = "";

    if (!itemRows.length) { section.style.display = "none"; return; }
    section.style.display = "";

    // Sort by sold qty descending by default
    itemRows.sort((a, b) => b.sold - a.sold);

    // Best seller stat card
    const best = itemRows[0];
    const existingBest = document.getElementById("stat-best-seller");
    if (!existingBest) {
      const d = document.createElement("div");
      d.className = "stat-card"; d.id = "stat-best-seller";
      d.innerHTML = `<div class="val" style="color:var(--purple);font-size:15px">${esc(best.name)}</div><div class="lbl">Best Seller (${num(best.sold)} sold)</div>`;
      statsEl.appendChild(d);
    }

    itemRows.forEach(r => {
      const tr = document.createElement("tr");
      const missing = r.missing;
      let missingHtml;
      if (missing > 0) {
        missingHtml = `<span class="badge neg-badge">⚠ missing ${num(missing)}</span>`;
      } else if (missing < 0) {
        missingHtml = `<span class="badge pos-badge">+${num(Math.abs(missing))} surplus</span>`;
      } else {
        missingHtml = `<span class="badge" style="background:var(--border);color:var(--muted)">balanced</span>`;
      }
      tr.innerHTML = `
        <td class="item-name">${esc(r.name)}</td>
        <td><span class="badge coin-badge">${num(r.sold)}</span></td>
        <td><span class="badge" style="color:var(--muted)">${num(r.bought)}</span></td>
        <td>${missingHtml}</td>`;
      itemTbody.appendChild(tr);
    });
  }

  render(activeMarket);
})();

// ══════════════════════════ LEDGER (v3 redesign) ═════════════════════════════
(function initLedger(){
  const DATA = (typeof ALL_EARNINGS !== "undefined" && ALL_EARNINGS) || {};
  const marketIds = Object.keys(DATA).filter(mid => DATA[mid] && DATA[mid].length > 0);
  const marketsEl = document.getElementById("lg-markets");
  const monthSel  = document.getElementById("lg-month");
  const q   = document.getElementById("lg-q");
  const flt = document.getElementById("lg-flt");
  const tbody = document.getElementById("lg-tbody");
  const emptyEl = document.getElementById("lg-empty");
  if (!marketsEl) return;
  let activeMarket = marketIds.includes("main") ? "main" : (marketIds[0] || null);
  let activeMonth = "all";
  let sortK = "net", sortDir = -1;
  let lineChart = null, donutChart = null;

  function mLabel(mid){ const m = (typeof MARKETS !== "undefined" && MARKETS[mid]); return m ? (m.name || mid) : (mid === "main" ? "Main" : mid); }
  function fmt(n){ n = Math.round(n || 0); return (n < 0 ? "-" : "") + Math.abs(n).toLocaleString(); }
  function shortK(n){ const a = Math.abs(n), s = n < 0 ? "-" : "+";
    if (a >= 1e6) return s + (a/1e6).toFixed(a >= 1e8 ? 0 : 1) + "M";
    if (a >= 1000) return s + (a/1000).toFixed(a >= 1e5 ? 0 : 1) + "k";
    return s + Math.round(a); }
  function esc2(s){ return (typeof esc === "function") ? esc(s) : String(s).replace(/</g,"&lt;"); }

  if (!marketIds.length) { if (emptyEl) emptyEl.style.display = ""; return; }

  if (marketIds.length > 1) {
    marketIds.forEach(mid => {
      const b = document.createElement("div");
      b.className = "lg-mtab" + (mid === activeMarket ? " active" : "");
      b.textContent = mLabel(mid);
      b.onclick = () => { activeMarket = mid; activeMonth = "all";
        marketsEl.querySelectorAll(".lg-mtab").forEach(t => t.classList.remove("active")); b.classList.add("active");
        buildMonths(); renderAll(); };
      marketsEl.appendChild(b);
    });
  } else if (activeMarket) {
    const b = document.createElement("div"); b.className = "lg-mtab active"; b.textContent = mLabel(activeMarket); marketsEl.appendChild(b);
  }

  function months(){ return DATA[activeMarket] || []; }
  function buildMonths(){
    const ms = months();
    monthSel.innerHTML = '<option value="all">All months (summary)</option>' +
      ms.slice().reverse().map(m => `<option value="${m.month}">${esc2(m.label)}</option>`).join("");
    monthSel.value = activeMonth;
  }
  function aggItems(ms){
    const a = {};
    ms.forEach(m => Object.entries(m.items || {}).forEach(([n, iv]) => {
      const e = a[n] || (a[n] = { item: n, sold: 0, bought: 0, net: 0 });
      e.sold += iv.sold || 0; e.bought += iv.bought || 0; e.net += iv.net || 0;
    }));
    return Object.values(a);
  }
  function currentItems(){
    const ms = months();
    if (activeMonth === "all") return aggItems(ms);
    const mo = ms.find(m => m.month === activeMonth);
    return mo ? aggItems([mo]) : [];
  }
  function sumSold(ms){ return ms.reduce((s, m) => s + Object.values(m.items || {}).reduce((x, iv) => x + (iv.sold || 0), 0), 0); }
  function totals(){
    const ms = months();
    if (activeMonth === "all") {
      return { inc: ms.reduce((s,m)=>s+m.income,0), sp: ms.reduce((s,m)=>s+m.spent,0),
               net: ms.reduce((s,m)=>s+m.net,0), label: "all months", prev: null, sold: sumSold(ms) };
    }
    const i = ms.findIndex(m => m.month === activeMonth), mo = ms[i];
    return { inc: mo.income, sp: mo.spent, net: mo.net, label: mo.label,
             prev: i > 0 ? ms[i-1].net : null, sold: sumSold([mo]) };
  }

  function renderAll(){
    const t = totals();
    document.getElementById("lg-heroLbl").textContent = "Net profit · " + t.label;
    const hn = document.getElementById("lg-heroNet");
    hn.textContent = (t.net >= 0 ? "+" : "") + fmt(t.net); hn.className = "lg-big " + (t.net >= 0 ? "lg-pos" : "lg-neg");
    const tr = document.getElementById("lg-heroTrend"), sub = document.getElementById("lg-heroSub");
    if (t.prev !== null && t.prev !== 0) {
      const pct = Math.round((t.net - t.prev) / Math.abs(t.prev) * 100);
      tr.style.display = ""; tr.textContent = (pct >= 0 ? "▲ " : "▼ ") + Math.abs(pct) + "%";
      tr.className = "lg-trend " + (pct >= 0 ? "up" : "down");
      sub.textContent = "vs " + (t.prev >= 0 ? "+" : "") + fmt(t.prev) + " prev month · ¢ coins";
    } else {
      tr.style.display = "none";
      sub.textContent = (activeMonth === "all" ? months().length + " month(s) tracked" : "first tracked month") + " · ¢ coins";
    }
    const marg = t.inc ? Math.round(t.net / t.inc * 100) : 0;
    const dn = document.getElementById("lg-donutNet"); dn.textContent = shortK(t.net); dn.className = "lg-dn " + (t.net >= 0 ? "lg-pos" : "lg-neg");
    document.getElementById("lg-donutPct").textContent = marg + "%";
    document.getElementById("lg-lgInc").textContent = fmt(t.inc);
    document.getElementById("lg-lgExp").textContent = fmt(t.sp);
    const items = currentItems();
    document.getElementById("lg-kSold").textContent = fmt(t.sold);
    document.getElementById("lg-kUniq").textContent = items.length;
    const sells = items.filter(i => i.net > 0).sort((a,b)=>b.net-a.net);
    const buys  = items.filter(i => i.net < 0).sort((a,b)=>a.net-b.net);
    const kTop = document.getElementById("lg-kTop"), kTopN = document.getElementById("lg-kTopN");
    if (sells[0]) { kTop.textContent = "+" + fmt(sells[0].net); kTopN.textContent = sells[0].item; } else { kTop.textContent = "—"; kTopN.textContent = "—"; }
    const kCost = document.getElementById("lg-kCost"), kCostN = document.getElementById("lg-kCostN");
    if (buys[0]) { kCost.textContent = fmt(buys[0].net); kCostN.textContent = buys[0].item; } else { kCost.textContent = "—"; kCostN.textContent = "none"; }
    const top = sells.slice(0, 7), maxS = Math.max.apply(null, top.map(s => s.net).concat([1]));
    document.getElementById("lg-leadLbl").textContent = "What's selling · " + t.label;
    document.getElementById("lg-lead").innerHTML = top.length
      ? top.map((s,i)=>`<div class="lg-lrow"><div class="lg-lrank">${i+1}</div><div><div class="lg-lname">${esc2(s.item)}</div><div class="lg-lbar"><span style="width:${Math.max(4, s.net/maxS*100)}%"></span></div></div><div class="lg-lmeta"><div class="lg-lrev lg-pos">+${fmt(s.net)}</div><div class="lg-lqty">${fmt(s.sold)} sold</div></div></div>`).join("")
      : '<div class="lg-lqty" style="padding:8px 2px">No sales this period.</div>';
    drawCharts(t);
    renderTable();
  }

  function drawCharts(t){
    const recent = months().slice(-12);
    if (lineChart) { lineChart.destroy(); lineChart = null; }
    lineChart = new Chart(document.getElementById("lg-lineChart"), { type: "line",
      data: { labels: recent.map(m => (m.label || "").slice(0, 3)),
        datasets: [{ data: recent.map(m => m.net), borderColor: "#22FF7A", borderWidth: 2, tension: .35, fill: true,
          backgroundColor: c => { const g = c.chart.ctx.createLinearGradient(0,0,0,150); g.addColorStop(0,"rgba(34,255,122,.28)"); g.addColorStop(1,"rgba(34,255,122,0)"); return g; },
          pointBackgroundColor: "#22FF7A", pointRadius: 3, pointHoverRadius: 6 }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => " net " + fmt(c.parsed.y) + " ¢" } } },
        scales: { x: { grid: { color: "rgba(255,255,255,.05)" }, ticks: { color: "#666", font: { family: "IBM Plex Mono" } } },
                  y: { grid: { color: "rgba(255,255,255,.05)" }, ticks: { color: "#666", font: { family: "IBM Plex Mono" }, callback: v => shortK(v) }, border: { display: false } } } } });
    if (donutChart) { donutChart.destroy(); donutChart = null; }
    donutChart = new Chart(document.getElementById("lg-donutChart"), { type: "doughnut",
      data: { labels: ["Income","Spent"], datasets: [{ data: [t.inc, t.sp], backgroundColor: ["#22FF7A","#FF4444"], borderColor: "#111", borderWidth: 3, cutout: "72%" }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => " " + c.label + " " + fmt(c.parsed) + " ¢" } } } } });
  }

  function renderTable(){
    let rows = currentItems();
    const term = (q.value || "").toLowerCase(), f = flt.value;
    rows = rows.filter(r => r.item.toLowerCase().includes(term));
    if (f === "income") rows = rows.filter(r => r.net > 0);
    if (f === "expense") rows = rows.filter(r => r.net < 0);
    rows.sort((a,b) => { let x = a[sortK], y = b[sortK]; if (typeof x === "string") return x.localeCompare(y) * sortDir; return (x - y) * sortDir; });
    if (!rows.length) { tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted)">No items match.</td></tr>'; return; }
    tbody.innerHTML = rows.map(r => { const c = r.net > 0 ? "lg-pos" : (r.net < 0 ? "lg-neg" : "lg-muted"), sg = r.net > 0 ? "+" : "";
      return `<tr><td class="item-name">${esc2(r.item)}</td><td>${fmt(r.sold)}</td><td>${fmt(r.bought)}</td><td class="${c}">${sg}${fmt(r.net)}</td></tr>`; }).join("");
  }

  q.addEventListener("input", renderTable);
  flt.addEventListener("change", renderTable);
  monthSel.addEventListener("change", () => { activeMonth = monthSel.value; renderAll(); });
  document.querySelectorAll('#page-earnings th[data-lgsort]').forEach(th => th.addEventListener("click", () => {
    const k = th.getAttribute("data-lgsort");
    if (sortK === k) sortDir = -sortDir; else { sortK = k; sortDir = (k === "item") ? 1 : -1; }
    renderTable();
  }));

  if (activeMarket) { buildMonths(); renderAll(); }
})();

// ══════════════════════════ INVENTORY (barrel fullness) ══════════════════════
(function initInventory(){
  const DATA = (typeof INVENTORY !== "undefined" && INVENTORY && INVENTORY.markets) || [];
  const tabsEl = document.getElementById("iv-markets");
  const search = document.getElementById("iv-search");
  const tbody  = document.getElementById("iv-tbody");
  const emptyEl= document.getElementById("iv-empty");
  const statsEl= document.getElementById("stats-inventory");
  if (!tbody) return;
  let active = 0, sortK = "pct", sortDir = 1;
  const fmt = n => Math.round(n || 0).toLocaleString();
  const esc2 = s => (typeof esc === "function") ? esc(s) : String(s).replace(/</g,"&lt;");
  const price = p => p > 0 ? (p < 1 ? p.toFixed(2) : fmt(p)) : "—";
  const fillColor = pct => pct <= 20 ? "var(--red)" : (pct < 60 ? "var(--amber)" : "var(--green)");
  const pctCls = pct => pct <= 20 ? "iv-neg" : (pct < 60 ? "iv-amb" : "iv-pos");
  if (!DATA.length) { if (emptyEl) emptyEl.style.display = ""; return; }

  DATA.forEach((m, i) => {
    const b = document.createElement("div");
    b.className = "iv-tab" + (i === active ? " active" : "");
    b.textContent = (m.name || m.market_id) + " · " + m.count;
    b.onclick = () => { active = i; tabsEl.querySelectorAll(".iv-tab").forEach(t => t.classList.remove("active")); b.classList.add("active"); render(); };
    tabsEl.appendChild(b);
  });

  function render(){
    const mk = DATA[active] || {};
    const items = mk.items || [];
    // Show the generate-orders button only for a market this viewer owns.
    const genBtn = document.getElementById("iv-genorders");
    if (genBtn) {
      const owns = window.OWNER && Array.isArray(window.OWNER.owned) && window.OWNER.owned.includes(String(mk.market_id));
      genBtn.style.display = owns ? "" : "none";
      genBtn.dataset.mid = mk.market_id || "";
    }
    const low = items.filter(x => x.capacity > 0 && x.pct <= 20).length;
    const totCap = items.reduce((s, x) => s + (x.capacity || 0), 0);
    const totStock = items.reduce((s, x) => s + (x.stock || 0), 0);
    const avg = totCap ? Math.round(100 * totStock / totCap) : 0;
    statsEl.innerHTML = "";
    [[items.length, "Items"], [String(low), "Low (≤20%)"], [avg + "%", "Avg fullness"]].forEach(([v, l], i) => {
      const d = document.createElement("div"); d.className = "stat-card";
      const col = (i === 1 && low > 0) ? "var(--red)" : (i === 2 ? (avg <= 20 ? "var(--red)" : (avg < 60 ? "var(--amber)" : "var(--green)")) : "var(--blue)");
      d.innerHTML = `<div class="val" style="color:${col}">${v}</div><div class="lbl">${l}</div>`;
      statsEl.appendChild(d);
    });
    const term = (search.value || "").toLowerCase();
    let rows = items.filter(x => (x.item || "").toLowerCase().includes(term));
    rows.sort((a, b) => { let x = a[sortK], y = b[sortK]; if (typeof x === "string") return x.localeCompare(y) * sortDir; return ((x || 0) - (y || 0)) * sortDir; });
    if (!rows.length) { tbody.innerHTML = items.length ? '<tr><td colspan="5" style="color:var(--muted)">No items match.</td></tr>' : ""; if (!items.length) emptyEl.style.display = ""; return; }
    emptyEl.style.display = "none";
    // Merge in the catalog/market price so Inventory doubles as the Prices view: use the
    // scanned listing price, else fall back to this market's derived price.
    const mp = (typeof MARKET_PRICES !== "undefined" && MARKET_PRICES[mk.market_id]) || {};
    tbody.innerHTML = rows.map(x => {
      const pct = Math.min(100, Math.max(0, x.pct || 0));
      const px = (x.price > 0) ? x.price : ((mp[x.item] && mp[x.item].coin) || 0);
      return `<tr><td class="item-name">${esc2(x.item)}</td>` +
        `<td><span class="iv-fill"><span style="width:${Math.max(2, pct)}%;background:${fillColor(x.pct)}"></span></span> <span class="${pctCls(x.pct)}">${Math.round(x.pct)}%</span></td>` +
        `<td>${fmt(x.stock)}</td><td>${fmt(x.capacity)}</td><td>${price(px)}</td></tr>`;
    }).join("");
  }

  search.addEventListener("input", render);
  const genBtn = document.getElementById("iv-genorders");
  const genMsg = document.getElementById("iv-genmsg");
  if (genBtn) genBtn.addEventListener("click", async () => {
    const mid = genBtn.dataset.mid;
    if (!mid || !window.OWNER || !window.OWNER.owned.includes(String(mid))) {
      if (genMsg) genMsg.textContent = "You can only generate orders for a market you own."; return;
    }
    const hdr = { "Content-Type": "application/json", "X-CSRF-Token": (window.OWNER.csrf || "") };
    genBtn.disabled = true; if (genMsg) genMsg.textContent = "Checking stock…";
    let prev;
    try {
      prev = await fetch("/api/owner/generate_orders", { method: "POST", headers: hdr,
        body: JSON.stringify({ market_id: mid, target_percent: 80, apply: false }) }).then(r => r.json());
    } catch (e) { prev = { ok: false, error: "network" }; }
    if (!prev || !prev.ok) { genBtn.disabled = false; if (genMsg) genMsg.textContent = (prev && prev.error) || "Failed."; return; }
    if (!prev.count) { genBtn.disabled = false; if (genMsg) genMsg.textContent = "Nothing to restock — all items at/above 80%."; return; }
    if (!confirm(`Create ${prev.count} restock order(s) to refill this market to 80%?`)) {
      genBtn.disabled = false; if (genMsg) genMsg.textContent = ""; return;
    }
    if (genMsg) genMsg.textContent = "Creating…";
    let res;
    try {
      res = await fetch("/api/owner/generate_orders", { method: "POST", headers: hdr,
        body: JSON.stringify({ market_id: mid, target_percent: 80, apply: true }) }).then(r => r.json());
    } catch (e) { res = { ok: false, error: "network" }; }
    genBtn.disabled = false;
    if (genMsg) genMsg.textContent = (res && res.ok) ? `✅ Created ${res.created} order(s) — see the Orders tab.` : ((res && res.error) || "Failed.");
  });
  document.querySelectorAll('#page-inventory th[data-ivsort]').forEach(th => th.addEventListener("click", () => {
    const k = th.getAttribute("data-ivsort");
    if (sortK === k) sortDir = -sortDir; else { sortK = k; sortDir = (k === "item" || k === "pct") ? 1 : -1; }
    document.querySelectorAll('#page-inventory th[data-ivsort]').forEach(t => {
      t.classList.remove("sorted");
      const a = t.querySelector(".sort-arrow"); if (a) a.textContent = "↕";
    });
    th.classList.add("sorted");
    const a = th.querySelector(".sort-arrow"); if (a) a.textContent = sortDir === 1 ? "↑" : "↓";
    render();
  }));
  render();
})();

// ══════════════════════════ ORDERS (restock board) ══════════════════════════
(function initOrders(){
  const DATA = (typeof ORDERS !== "undefined" && ORDERS && ORDERS.markets) || [];
  const tabsEl = document.getElementById("or-markets");
  const tbody  = document.getElementById("or-tbody");
  const emptyEl= document.getElementById("or-empty");
  const statsEl= document.getElementById("stats-orders");
  if (!tbody) return;
  let active = 0;
  const fmt = n => Math.round(n || 0).toLocaleString();
  const esc2 = s => (typeof esc === "function") ? esc(s) : String(s).replace(/</g,"&lt;");
  if (!DATA.length) { if (emptyEl) emptyEl.style.display = ""; return; }

  DATA.forEach((m, i) => {
    const b = document.createElement("div");
    b.className = "or-tab" + (i === active ? " active" : "");
    b.textContent = (m.name || m.market_id) + " · " + m.count;
    b.onclick = () => { active = i; tabsEl.querySelectorAll(".or-tab").forEach(t => t.classList.remove("active")); b.classList.add("active"); render(); };
    tabsEl.appendChild(b);
  });

  function statusTag(st, claimed, req){
    if (st === "open" && claimed > 0 && claimed < req) st = "partial";
    const cls = st === "open" ? "or-open" : (st === "partial" ? "or-partial" : "or-claimed");
    return `<span class="or-tag ${cls}">${st}</span>`;
  }

  function render(){
    const orders = (DATA[active] || {}).orders || [];
    const totalReq = orders.reduce((s, o) => s + (o.requested || 0), 0);
    const openN = orders.filter(o => (o.claimed || 0) < (o.requested || 0)).length;
    statsEl.innerHTML = "";
    [[orders.length, "Open orders"], [fmt(totalReq), "Pieces requested"], [String(openN), "Still need workers"]].forEach(([v, l], i) => {
      const d = document.createElement("div"); d.className = "stat-card";
      const col = i === 2 && openN > 0 ? "var(--amber)" : "var(--blue)";
      d.innerHTML = `<div class="val" style="color:${col}">${v}</div><div class="lbl">${l}</div>`;
      statsEl.appendChild(d);
    });
    if (!orders.length) { tbody.innerHTML = ""; emptyEl.style.display = ""; return; }
    emptyEl.style.display = "none";
    tbody.innerHTML = orders.map(o => {
      const req = o.requested || 0, cl = Math.min(o.claimed || 0, req), pct = req ? Math.round(100 * cl / req) : 0;
      return `<tr><td>#${o.id}</td><td class="item-name">${esc2(o.item)}</td>` +
        `<td>${fmt(req)}</td><td>${fmt(o.claimed || 0)}</td>` +
        `<td><span class="or-fill"><span style="width:${pct}%"></span></span> ${pct}%</td>` +
        `<td>${statusTag(o.status, o.claimed || 0, req)}</td></tr>`;
    }).join("");
  }
  render();
})();

// ══════════════════════════ PLACE ORDER (cart) ═══════════════════════════════
(function initOrderForm(){
  const lock=document.getElementById("or-place-locked"), form=document.getElementById("or-place-form");
  if(!form) return;
  const dl=document.getElementById("or-catalog"), itemIn=document.getElementById("or-item"),
        qtyIn=document.getElementById("or-qty"), addBtn=document.getElementById("or-add"),
        addMsg=document.getElementById("or-add-msg"), cartBody=document.getElementById("or-cart"),
        cartTbl=document.getElementById("or-cart-tbl"), notesIn=document.getElementById("or-notes"),
        subBtn=document.getElementById("or-submit"), subMsg=document.getElementById("or-submit-msg");
  const escg = s => (typeof esc==="function") ? esc(s) : String(s).replace(/</g,"&lt;");
  const fmt = n => Math.round(n||0).toLocaleString();
  let priceMap={}, nameSet={}, cart=[];

  fetch("/api/items").then(r=>r.json()).then(items=>{
    const names=Object.keys(items||{}).sort((a,b)=>a.localeCompare(b));
    dl.innerHTML=names.map(n=>`<option value="${escg(n)}">`).join("");
    names.forEach(n=>{ nameSet[n.toLowerCase()]=n; priceMap[n]=(items[n]&&items[n].coin)||0; });
  }).catch(()=>{});

  // Show the form only to logged-in users.
  (window.OWNER_READY||Promise.resolve()).then(()=>fetch("/api/me").then(r=>r.json())).then(me=>{
    if(me&&me.logged_in){ lock.style.display="none"; form.style.display=""; }
  }).catch(()=>{});

  function renderCart(){
    if(!cart.length){ cartTbl.style.display="none"; cartBody.innerHTML=""; return; }
    cartTbl.style.display="";
    cartBody.innerHTML=cart.map((c,i)=>`<tr><td class="item-name">${escg(c.item)}</td><td>${fmt(c.qty)}</td><td>${fmt((priceMap[c.item]||0)*c.qty)}</td><td><span data-rm="${i}" style="cursor:pointer;color:var(--muted)">✕</span></td></tr>`).join("");
    cartBody.querySelectorAll("[data-rm]").forEach(x=>x.onclick=()=>{ cart.splice(+x.getAttribute("data-rm"),1); renderCart(); });
  }
  function addToCart(){
    addMsg.textContent="";
    const raw=(itemIn.value||"").trim(), real=nameSet[raw.toLowerCase()], qty=parseInt(qtyIn.value||"0",10);
    if(!real){ addMsg.textContent="Pick an item from the list."; return; }
    if(!qty||qty<=0){ addMsg.textContent="Enter a quantity."; return; }
    const ex=cart.find(c=>c.item===real);
    if(ex) ex.qty+=qty; else cart.push({item:real,qty:qty});
    itemIn.value=""; qtyIn.value="1"; itemIn.focus(); renderCart();
  }
  addBtn.onclick=addToCart;
  itemIn.addEventListener("keydown",e=>{ if(e.key==="Enter"){ e.preventDefault(); addToCart(); }});
  subBtn.onclick=async()=>{
    subMsg.textContent="";
    if(!cart.length){ subMsg.style.color="var(--amber)"; subMsg.textContent="Add at least one item."; return; }
    const csrf=(window.OWNER&&window.OWNER.csrf)||"";
    subBtn.disabled=true; subMsg.style.color="var(--muted)"; subMsg.textContent="Sending…";
    let res;
    try{
      res=await fetch("/api/order",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},
        body:JSON.stringify({items:cart.map(c=>({item:c.item,qty:c.qty})),notes:(notesIn.value||"").trim()})}).then(r=>r.json());
    }catch(e){ res={ok:false,error:"network error"}; }
    subBtn.disabled=false;
    if(res&&res.ok){ subMsg.style.color="var(--green)"; subMsg.textContent=`✅ Order #${res.order_id} sent — a manager will review it.`; cart=[]; notesIn.value=""; renderCart(); }
    else{ subMsg.style.color="var(--amber)"; subMsg.textContent=(res&&res.error)||"Failed."; }
  };
})();

// ══════════════════════════ STOCKS ═══════════════════════════════════════════
(function initStocks() {
  const markets = (STOCKS && STOCKS.markets) || [];

  // ── Abexilas Market Index ──
  (function renderIndex() {
    const idx = STOCKS && STOCKS.index;
    const card = document.getElementById("index-card");
    if (!card) return;
    if (!idx || !idx.history || !idx.history.length) { card.style.display = "none"; return; }
    card.style.display = "";
    document.getElementById("index-value").textContent = num(idx.value);
    const up = idx.change_pct >= 0;
    const chg = document.getElementById("index-change");
    chg.textContent = (up ? "\u25B2 " : "\u25BC ") + Math.abs(idx.change_pct).toFixed(2) + "%";
    chg.className = "t-chg " + (up ? "up" : "down");
    document.getElementById("index-mcap").textContent = num(idx.total_mcap) + " \u00A2";
    document.getElementById("index-markets").textContent = idx.markets;
    const ctx = document.getElementById("index-chart");
    const grad = ctx.getContext("2d").createLinearGradient(0, 0, 0, 220);
    grad.addColorStop(0, "rgba(34,255,122,.2)");
    grad.addColorStop(1, "rgba(34,255,122,0)");
    new Chart(ctx, {
      type: "line",
      data: { labels: idx.history.map(h => (h.t || "").slice(5, 16).replace("T", " ")),
        datasets: [{ label: "Index", data: idx.history.map(h => h.v),
          borderColor: "#22FF7A", backgroundColor: grad, fill: true,
          borderWidth: 1.5, tension: 0.35, pointRadius: 0, pointHoverRadius: 3,
          pointHoverBackgroundColor: "#22FF7A", pointHoverBorderColor: "#0A0A0A", pointHoverBorderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false },
          tooltip: { backgroundColor: "#111111", borderColor: "#1E1E1E", borderWidth: 1,
            titleColor: "#666666", bodyColor: "#F0F0F0", padding: 9, cornerRadius: 2, displayColors: false,
            bodyFont: { family: "IBM Plex Mono" }, callbacks: { label: c => Number(c.parsed.y).toLocaleString() } } },
        scales: { x: { ticks: { color: "#666666", maxTicksLimit: 8 }, grid: { display: false }, border: { display: false } },
          y: { ticks: { color: "#666666", callback: v => Number(v).toLocaleString() }, grid: { color: "rgba(255,255,255,.05)" }, border: { display: false } } } },
    });
  })();

  function divCell(m) {
    if (!m.div_pct) return "—";
    const y = m.div_yield ? ` <span style="color:var(--muted)">· ${m.div_yield.toFixed(1)}% yld</span>` : "";
    return `<span style="color:var(--purple);font-weight:600">${m.div_pct.toFixed(0)}%</span>${y}`;
  }

  function sparkline(m) {
    const h = (m.history || []).map(p => p.price).filter(v => v != null);
    if (h.length < 2) return "";
    const w = 84, ht = 24, lo = Math.min(...h), hi = Math.max(...h), rng = (hi - lo) || 1;
    const step = w / (h.length - 1);
    const pts = h.map((v, i) => `${(i * step).toFixed(1)},${(ht - 2 - ((v - lo) / rng) * (ht - 4)).toFixed(1)}`).join(" ");
    const col = m.change >= 0 ? "#22FF7A" : "#FF4444";
    return `<svg width="${w}" height="${ht}" viewBox="0 0 ${w} ${ht}" fill="none" style="display:block"><polyline points="${pts}" stroke="${col}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  }

  // Ticker strip
  const ticker = document.getElementById("stock-ticker");
  markets.forEach(m => {
    const cls = m.change >= 0 ? "up" : "down";
    const arrow = m.change >= 0 ? "▲" : "▼";
    const d = document.createElement("div");
    d.className = "tick";
    d.innerHTML = `<div class="t-name">${esc(m.ticker)} · ${esc(m.name)}</div>
      <div class="t-price">${num(m.price.toFixed(2))} ¢</div>
      <div class="t-chg ${cls}">${arrow} ${m.pct.toFixed(2)}%</div>`;
    ticker.appendChild(d);
  });

  // Stats
  const statsEl = document.getElementById("stats-stocks");
  const totalMcap = markets.reduce((s, m) => s + m.mcap, 0);
  const mover = markets.slice().sort((a, b) => Math.abs(b.pct) - Math.abs(a.pct))[0];
  [
    [markets.length,                       "Public Markets"],
    [num(Math.round(totalMcap)) + " ¢",    "Total Market Cap"],
    [num(Math.round(markets.reduce((s,m)=>s+(m.treasury||0),0))) + " ¢", "Total Treasury"],
    [mover ? `${mover.name} ${mover.pct >= 0 ? "+" : ""}${mover.pct.toFixed(1)}%` : "—", "Top Mover"],
  ].forEach(([v, l]) => {
    const d = document.createElement("div");
    d.className = "stat-card";
    d.innerHTML = `<div class="val" style="font-size:16px">${v}</div><div class="lbl">${l}</div>`;
    statsEl.appendChild(d);
  });

  // Table
  const tbody = document.getElementById("stocks-tbody");
  const emptyEl = document.getElementById("stocks-empty");
  emptyEl.style.display = markets.length ? "none" : "";
  markets.forEach(m => {
    const cls = m.change >= 0 ? "up" : "down";
    const arrow = m.change >= 0 ? "▲" : "▼";
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    tr.innerHTML = `
      <td class="item-name">${mktDot(m.mid)}${esc(m.name)} <span class="badge market-tag">${esc(m.ticker)}</span></td>
      <td><span class="badge coin-badge">${num(m.price.toFixed(2))} ¢</span></td>
      <td class="${cls}">${arrow} ${m.pct.toFixed(2)}%</td>
      <td>${sparkline(m)}</td>
      <td>${num(Math.round(m.mcap))} ¢</td>
      <td>${m.pe.toFixed(1)}x</td>
      <td>${divCell(m)}</td>
      <td>${m.treasury ? num(Math.round(m.treasury)) + " ¢" : "—"}</td>
      <td><span class="${(m.backing_pct||0) >= 25 ? "up" : "down"}">${(m.backing_pct||0).toFixed(0)}%</span></td>
      <td>${m.holders_count}</td>`;
    tr.addEventListener("click", () => select(m.mid));
    tbody.appendChild(tr);
  });

  // Market selector tabs
  const tabsEl = document.getElementById("stock-market-tabs");
  markets.forEach((m, i) => {
    const b = document.createElement("button");
    b.className = "tab" + (i === 0 ? " active" : "");
    b.innerHTML = mktDot(m.mid) + esc(m.name);
    b.addEventListener("click", () => select(m.mid));
    tabsEl.appendChild(b);
  });

  let chart = null;
  function select(mid) {
    const m = markets.find(x => x.mid === mid);
    if (!m) return;
    [...tabsEl.children].forEach(b => b.classList.toggle("active", b.textContent === m.name));
    document.getElementById("stock-chart-title").textContent = "Share price history — " + m.name;
    const ctx = document.getElementById("stock-chart");
    if (chart) chart.destroy();
    const labels = m.history.map(h => (h.t || "").slice(5, 16).replace("T", " "));
    const cctx = ctx.getContext("2d");
    const grad = cctx.createLinearGradient(0, 0, 0, 300);
    grad.addColorStop(0, "rgba(34,255,122,.22)");
    grad.addColorStop(1, "rgba(34,255,122,0)");
    chart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets: [{
        label: "Price", data: m.history.map(h => h.price),
        borderColor: "#22FF7A", backgroundColor: grad, fill: true,
        borderWidth: 1.5, tension: 0.35, pointRadius: 0, pointHoverRadius: 3,
        pointHoverBackgroundColor: "#22FF7A", pointHoverBorderColor: "#0A0A0A", pointHoverBorderWidth: 2,
      }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "#111111", borderColor: "#1E1E1E", borderWidth: 1,
            titleColor: "#666666", bodyColor: "#F0F0F0", padding: 9, cornerRadius: 2, displayColors: false, bodyFont: { family: "IBM Plex Mono" },
            callbacks: { label: c => Number(c.parsed.y).toLocaleString() + " ¢" },
          },
        },
        scales: {
          x: { ticks: { color: "#666666", maxTicksLimit: 8 }, grid: { display: false }, border: { display: false } },
          y: { ticks: { color: "#666666", callback: v => Number(v).toLocaleString() }, grid: { color: "rgba(255,255,255,.05)" }, border: { display: false } },
        },
      },
    });
    const sec = document.getElementById("holders-section");
    const htb = document.getElementById("holders-tbody");
    document.getElementById("holders-market-name").textContent = m.name;
    const _ctl = document.getElementById("captable-link");
    if (_ctl) _ctl.href = "/shares/" + encodeURIComponent(m.mid);
    htb.innerHTML = "";
    if (m.top_holders && m.top_holders.length) {
      sec.style.display = "";
      m.top_holders.forEach((h, i) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${i + 1}</td><td class="item-name">${esc(h.id)}</td>
          <td>${num(Math.round(h.shares))}</td>
          <td><span class="badge coin-badge">${num(Math.round(h.value))} ¢</span></td>`;
        htb.appendChild(tr);
      });
    } else {
      sec.style.display = "none";
    }
  }
  // Dividends & Treasury table
  (function renderDividends() {
    const sec = document.getElementById("dividends-section");
    const tb = document.getElementById("dividends-tbody");
    if (!markets.length) { sec.style.display = "none"; return; }
    sec.style.display = "";
    markets.forEach(m => {
      const ld = m.last_div;
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="item-name">${mktDot(m.mid)}${esc(m.name)} <span class="badge market-tag">${esc(m.ticker)}</span></td>
        <td>${m.div_pct ? `<span style="color:var(--purple);font-weight:600">${m.div_pct.toFixed(0)}%</span>` : "—"}</td>
        <td>${ld ? num(Math.round(ld.total)) + " ¢ · " + esc(ld.month) : "—"}</td>
        <td>${ld ? num(ld.per_share.toFixed(2)) + " ¢" : "—"}</td>
        <td>${m.treasury ? num(Math.round(m.treasury)) + " ¢" : "—"}</td>
        <td>${m.open_orders || 0}</td>`;
      tb.appendChild(tr);
    });
  })();

  if (markets[0]) select(markets[0].mid);
})();

// ══════════════════════════ AUTH (Discord-code login) ════════════════════════
(function initAuth() {
  const area = document.getElementById("auth-area");

  function render(me) {
    area.innerHTML = "";
    if (me && me.logged_in) {
      const span = document.createElement("span");
      span.innerHTML = `👤 <span class="auth-name">${esc(me.name || "You")}</span>`;
      const anonBtn = document.createElement("button");
      anonBtn.className = "auth-btn ghost";
      anonBtn.textContent = me.anonymous ? "Anonymous: ON" : "Anonymous: off";
      anonBtn.title = "Hide your name from the public holders leaderboard";
      anonBtn.onclick = async () => {
        try {
          const r = await fetch("/api/anon", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ anonymous: !me.anonymous }),
          });
          const j = await r.json();
          if (j.ok) { me.anonymous = j.anonymous; render(me); }
        } catch (e) {}
      };
      const out = document.createElement("button");
      out.className = "auth-btn ghost";
      out.textContent = "Log out";
      out.onclick = async () => { try { await fetch("/api/logout", { method: "POST" }); } catch (e) {} location.reload(); };
      area.appendChild(span); area.appendChild(anonBtn); area.appendChild(out);
      renderHoldings(me);
    } else {
      const btn = document.createElement("button");
      btn.className = "auth-btn";
      btn.textContent = "Log in";
      btn.onclick = login;
      area.appendChild(btn);
      const card = document.getElementById("my-holdings-card");
      if (card) card.style.display = "none";
    }
  }

  async function login() {
    const code = (window.prompt("Run /website_login in Discord, then paste your code here:") || "").trim();
    if (!code) return;
    try {
      const r = await fetch("/api/link", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
      });
      const j = await r.json();
      if (j.ok) location.reload();
      else alert(j.error || "Login failed.");
    } catch (e) { alert("Login failed."); }
  }

  function renderHoldings(me) {
    const card = document.getElementById("my-holdings-card");
    const tb = document.getElementById("my-holdings-tbody");
    const sub = document.getElementById("my-holdings-sub");
    if (!card || !tb) return;
    const p = me.portfolio || [];
    if (!p.length) { card.style.display = "none"; return; }
    card.style.display = "";
    tb.innerHTML = "";
    let tv = 0, tc = 0;
    p.forEach(h => {
      const pl = h.value - h.cost; tv += h.value; tc += h.cost;
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="item-name">${mktDot(h.market)}${esc(h.market)}</td>
        <td>${num(Math.round(h.shares))}</td>
        <td><span class="badge coin-badge">${num(Math.round(h.value))} ¢</span></td>
        <td>${num(Math.round(h.cost))} ¢</td>
        <td class="${pl >= 0 ? "up" : "down"}">${pl >= 0 ? "+" : ""}${num(Math.round(pl))} ¢</td>`;
      tb.appendChild(tr);
    });
    if (sub) sub.textContent = `· total ${num(Math.round(tv))} ¢ (${tv - tc >= 0 ? "+" : ""}${num(Math.round(tv - tc))} ¢)`;
  }

  fetch("/api/me").then(r => r.json()).then(render).catch(() => render({ logged_in: false }));
})();


// ══════════════════════════ MY MARKET (owner panel) ══════════════════════════
(function initOwner() {
  let curMid = null;
  let csrf = "";
  async function post(url, body) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify(body) });
      return await r.json();
    } catch (e) { return { ok: false, error: "network" }; }
  }
  function statCard(val, lbl) {
    const d = document.createElement("div");
    d.className = "stat-card";
    d.innerHTML = `<div class="val" style="font-size:18px">${val}</div><div class="lbl">${lbl}</div>`;
    return d;
  }
  async function loadLoyalty(mid) {
    const mult = document.getElementById("loy-mult"), bonus = document.getElementById("loy-bonus");
    const pct = document.getElementById("loy-pct");
    if (!mult || !bonus) return;
    try {
      const r = await fetch("/api/owner/loyalty?market_id=" + encodeURIComponent(mid)).then(x => x.json());
      if (r && r.ok) { mult.value = r.pts_mult; bonus.value = r.coin_bonus; if (pct) pct.value = r.pct_bonus || 0; }
    } catch (e) {}
  }
  let obSaveTimer = null;
  async function loadOrderBuilder(mid) {
    const catsEl = document.getElementById("ob-cats");
    const emptyEl = document.getElementById("ob-empty");
    const buildBtn = document.getElementById("ob-build");
    const msgEl = document.getElementById("ob-msg");
    if (!catsEl) return;
    catsEl.innerHTML = ""; if (msgEl) msgEl.textContent = "";
    let r;
    try { r = await fetch("/api/owner/catalog?market_id=" + encodeURIComponent(mid)).then(x => x.json()); }
    catch (e) { r = { ok: false }; }
    if (!r || !r.ok) { if (emptyEl) emptyEl.style.display = ""; return; }
    const cats = r.categories || {};
    const names = Object.keys(cats);
    if (emptyEl) emptyEl.style.display = names.length ? "none" : "";
    function scheduleSave(item, patch) {
      clearTimeout(obSaveTimer);
      obSaveTimer = setTimeout(() => {
        post("/api/owner/set_target", Object.assign({ market_id: mid, item: item }, patch));
      }, 500);
    }
    // Collapsed-by-default accordion: each category is a clickable header showing its
    // fullness % and how many items are ticked, so you can scan health without expanding.
    names.sort();
    names.forEach(cat => {
      const rows = cats[cat] || [];
      if (!rows.length) return;
      let cap = 0, stk = 0, ticked = 0;
      rows.forEach(r => { cap += (r.capacity || 0); stk += (r.stock || 0); if (r.tracked) ticked++; });
      const fullPct = cap > 0 ? Math.round(100 * stk / cap) : 0;
      const fullCol = fullPct <= 20 ? "var(--down)" : (fullPct < 60 ? "#E8B339" : "var(--accent)");
      const wrap = document.createElement("div");
      wrap.style.marginBottom = "2px";
      const header = document.createElement("div");
      header.style.cssText = "cursor:pointer;user-select:none;display:flex;align-items:center;gap:8px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;padding:9px 0;border-bottom:1px solid var(--border)";
      const caret = document.createElement("span");
      caret.textContent = "▸"; caret.style.cssText = "display:inline-block;width:12px";
      const lbl = document.createElement("span");
      lbl.style.flex = "1"; lbl.textContent = cat + " (" + rows.length + ")";
      const meta = document.createElement("span");
      meta.style.textTransform = "none";
      meta.innerHTML = (cap > 0 ? '<span style="color:' + fullCol + '">' + fullPct + '% full</span>' : "")
                     + (ticked ? ' · <span style="color:var(--accent)">' + ticked + ' ticked</span>' : "");
      header.appendChild(caret); header.appendChild(lbl); header.appendChild(meta);
      const body = document.createElement("div");
      body.style.display = "none";
      rows.forEach(row => {
        const line = document.createElement("div");
        line.style.cssText = "display:flex;align-items:center;gap:10px;padding:5px 0 5px 20px;border-bottom:1px solid var(--border)";
        const cb = document.createElement("input");
        cb.type = "checkbox"; cb.checked = !!row.tracked;
        cb.title = "Track this item in the order builder";
        cb.onchange = () => scheduleSave(row.item, { tracked: cb.checked });
        const name = document.createElement("span");
        name.textContent = row.display || row.item;   // cleaned name; API calls still use row.item
        name.title = row.item;                         // hover shows the raw catalog name
        name.style.cssText = "flex:1;min-width:0";
        const stockSpan = document.createElement("span");
        stockSpan.style.cssText = "color:var(--muted);font-size:11.5px;min-width:90px;text-align:right";
        stockSpan.textContent = num(row.stock) + " / " + num(row.capacity);
        const pctIn = document.createElement("input");
        pctIn.type = "number"; pctIn.min = 0; pctIn.max = 100;
        pctIn.value = Math.round(row.target_pct); pctIn.className = "ownin"; pctIn.style.width = "64px";
        pctIn.oninput = () => scheduleSave(row.item, { target_pct: Number(pctIn.value) });
        const pctLbl = document.createElement("span");
        pctLbl.textContent = "%"; pctLbl.style.color = "var(--muted)";
        line.appendChild(cb); line.appendChild(name); line.appendChild(stockSpan);
        line.appendChild(pctIn); line.appendChild(pctLbl);
        body.appendChild(line);
      });
      header.onclick = () => {
        const open = body.style.display !== "none";
        body.style.display = open ? "none" : "";
        caret.textContent = open ? "▸" : "▾";
      };
      wrap.appendChild(header); wrap.appendChild(body);
      catsEl.appendChild(wrap);
    });
    if (buildBtn) {
      buildBtn.onclick = async () => {
        buildBtn.disabled = true; if (msgEl) msgEl.textContent = "Checking…";
        let prev;
        try { prev = await post("/api/owner/build_order", { market_id: mid, apply: false }); }
        catch (e) { prev = { ok: false, error: "network" }; }
        if (!prev || !prev.ok) { buildBtn.disabled = false; if (msgEl) msgEl.textContent = (prev && prev.error) || "Failed."; return; }
        if (!prev.count) { buildBtn.disabled = false; if (msgEl) msgEl.textContent = "Nothing to restock — every ticked item is at or above its target."; return; }
        if (!confirm(`Create ${prev.count} restock order(s) for your ticked items?`)) {
          buildBtn.disabled = false; if (msgEl) msgEl.textContent = ""; return;
        }
        if (msgEl) msgEl.textContent = "Creating…";
        let res;
        try { res = await post("/api/owner/build_order", { market_id: mid, apply: true }); }
        catch (e) { res = { ok: false, error: "network" }; }
        buildBtn.disabled = false;
        if (msgEl) msgEl.textContent = (res && res.ok) ? `✅ Created ${res.created} order(s) — see the Orders tab.` : ((res && res.error) || "Failed.");
      };
    }
  }
  async function loadInv(mid) {
    curMid = mid;
    loadLoyalty(mid);
    loadOrderBuilder(mid);
    [...document.getElementById("owner-market-tabs").children].forEach(b => b.classList.toggle("active", b.dataset.mid === mid));
    const tb = document.getElementById("owner-tbody");
    const dl = document.getElementById("owner-itemlist");
    const st = document.getElementById("owner-stats");
    tb.innerHTML = ""; dl.innerHTML = ""; st.innerHTML = "";
    let r;
    try { r = await fetch("/api/owner/inventory?market_id=" + encodeURIComponent(mid)).then(x => x.json()); }
    catch (e) { r = { ok: false }; }
    if (!r || !r.ok) { document.getElementById("owner-empty").style.display = ""; return; }
    const items = r.items || [];
    const totSold = items.reduce((s, i) => s + (i.sold || 0), 0);
    st.appendChild(statCard(items.length, "Items"));
    st.appendChild(statCard(num(totSold), "Units sold (CSN)"));
    st.appendChild(statCard(num(items.filter(i => i.in_catalog).length), "In catalog"));
    document.getElementById("owner-empty").style.display = items.length ? "none" : "";
    // Group the price/stock table by category into collapsed-by-default sections (matches
    // the Order builder). The datalist still lists every item so the manual-restock box
    // autocompletes regardless of which sections are expanded.
    const groups = {};
    items.forEach(it => {
      const opt = document.createElement("option"); opt.value = it.item; dl.appendChild(opt);
      const c = it.category || "Misc";
      (groups[c] = groups[c] || []).push(it);
    });
    Object.keys(groups).sort().forEach(cat => {
      const rows = groups[cat];
      const catId = "oc_" + cat.replace(/[^A-Za-z0-9]/g, "");
      const htr = document.createElement("tr");
      htr.style.cursor = "pointer"; htr.dataset.open = "0";
      const htd = document.createElement("td"); htd.colSpan = 6;
      htd.style.cssText = "color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:11px;padding:9px 0;user-select:none";
      htd.innerHTML = '<span class="oc-caret" style="display:inline-block;width:14px">▸</span>' + esc(cat) + " (" + rows.length + ")";
      htr.appendChild(htd);
      htr.onclick = () => {
        const open = htr.dataset.open === "1";
        htr.dataset.open = open ? "0" : "1";
        const cr = htd.querySelector(".oc-caret"); if (cr) cr.textContent = open ? "▸" : "▾";
        tb.querySelectorAll('tr[data-cat="' + catId + '"]').forEach(r => { r.style.display = open ? "none" : ""; });
      };
      tb.appendChild(htr);
      rows.forEach(it => {
        const tr = document.createElement("tr");
        tr.dataset.cat = catId; tr.style.display = "none";
        const tdName = document.createElement("td"); tdName.className = "item-name";
        tdName.textContent = it.display || it.item; tdName.title = it.item;   // cleaned name; raw is the save key
        const tdStock = document.createElement("td");
        const stockIn = document.createElement("input"); stockIn.className = "own-price"; stockIn.type = "number"; stockIn.value = Math.round(it.stock); stockIn.style.width = "80px"; stockIn.title = "Editable stock — set the real amount (or 0), then Save";
        tdStock.appendChild(stockIn);
        const tdPrice = document.createElement("td");
        const price = document.createElement("input"); price.className = "own-price"; price.type = "number"; price.step = "any"; price.value = Math.round((it.coin || 0) * 100) / 100; price.style.width = "92px";
        tdPrice.appendChild(price);
        const tdSold = document.createElement("td"); tdSold.textContent = num(it.sold);
        const tdOpt = document.createElement("td");
        const optSpan = document.createElement("span"); optSpan.style.color = "var(--purple)"; optSpan.style.fontWeight = "600"; optSpan.textContent = num(it.suggested) + " ¢";
        const useBtn = document.createElement("button"); useBtn.className = "mini-btn"; useBtn.textContent = "use"; useBtn.style.marginLeft = "8px";
        useBtn.onclick = () => { price.value = it.suggested; };
        tdOpt.appendChild(optSpan); tdOpt.appendChild(useBtn);
        const tdAct = document.createElement("td");
        const saveBtn = document.createElement("button"); saveBtn.className = "mini-btn"; saveBtn.textContent = "Save";
        saveBtn.onclick = async () => {
          saveBtn.textContent = "…";
          const res = await post("/api/owner/set_item", { market_id: mid, item: it.item, coin: Number(price.value), stock: Number(stockIn.value) });
          saveBtn.textContent = (res && res.ok) ? "Saved" : "Err";
          setTimeout(() => { saveBtn.textContent = "Save"; }, 1200);
        };
        const rmBtn = document.createElement("button"); rmBtn.className = "mini-btn danger"; rmBtn.textContent = "Remove"; rmBtn.style.marginLeft = "6px";
        rmBtn.onclick = async () => {
          if (!confirm(`Remove "${it.item}" from this market?\n\nFull remove also adjusts historical net and your share price.`)) return;
          await post("/api/owner/remove_item", { market_id: mid, item: it.item, mode: "full" });
          loadInv(mid);
        };
        tdAct.appendChild(saveBtn); tdAct.appendChild(rmBtn);
        tr.appendChild(tdName); tr.appendChild(tdStock); tr.appendChild(tdPrice); tr.appendChild(tdSold); tr.appendChild(tdOpt); tr.appendChild(tdAct);
        tb.appendChild(tr);
      });
    });
  }
  const loySave = document.getElementById("loy-save");
  if (loySave) loySave.onclick = async () => {
    const msg = document.getElementById("loy-msg");
    const pm = Number(document.getElementById("loy-mult").value);
    const cb = Number(document.getElementById("loy-bonus").value);
    const pctEl = document.getElementById("loy-pct");
    const pct = pctEl ? Number(pctEl.value || 0) : 0;
    if (!(pm > 0)) { msg.textContent = "Multiplier must be > 0."; return; }
    if (cb < 0 || pct < 0) { msg.textContent = "Bonuses can't be negative."; return; }
    msg.textContent = "Saving…";
    const res = await post("/api/owner/set_loyalty", { market_id: curMid, pts_mult: pm, coin_bonus: cb, pct_bonus: pct });
    msg.textContent = (res && res.ok) ? "Saved ✓" : ((res && res.error) || "Failed.");
  };
  const futSend = document.getElementById("fut-send");
  if (futSend) futSend.onclick = async () => {
    const msg = document.getElementById("fut-msg");
    const items = document.getElementById("fut-items").value.trim();
    const notes = document.getElementById("fut-notes").value.trim();
    if (!items) { msg.textContent = "Paste at least one item (one per line)."; return; }
    futSend.disabled = true; msg.textContent = "Submitting…";
    const res = await post("/api/owner/futures", { market_id: curMid, items, notes });
    futSend.disabled = false;
    if (res && res.ok) {
      msg.textContent = `✅ Futures request #${res.bulk_id} sent (${res.count} item(s)) — a manager will approve it.`;
      document.getElementById("fut-items").value = ""; document.getElementById("fut-notes").value = "";
    } else {
      msg.textContent = (res && res.error) || "Failed.";
    }
  };
  const addBtn = document.getElementById("rs-add");
  if (addBtn) addBtn.onclick = async () => {
    const item = document.getElementById("rs-item").value.trim();
    const qty = Number(document.getElementById("rs-qty").value);
    const cost = Number(document.getElementById("rs-cost").value);
    const msg = document.getElementById("rs-msg");
    if (!item || !qty || qty < 1) { msg.textContent = "Enter an item and quantity."; return; }
    msg.textContent = "Saving…";
    const res = await post("/api/owner/log_restock", { market_id: curMid, item, qty, cost: cost || 0 });
    msg.textContent = (res && res.ok) ? `Added ${qty}× ${item}.` : ((res && res.error) || "Failed.");
    if (res && res.ok) {
      document.getElementById("rs-item").value = ""; document.getElementById("rs-qty").value = ""; document.getElementById("rs-cost").value = "";
      loadInv(curMid);
    }
  };
  fetch("/api/me").then(r => r.json()).then(me => {
    csrf = (me && me.csrf) || "";
    const owned = (me && me.owned) || [];
    if (!owned.length) return;
    document.getElementById("nav-mymarket").style.display = "";
    const tabs = document.getElementById("owner-market-tabs");
    owned.forEach((mk, i) => {
      const b = document.createElement("button");
      b.className = "tab" + (i === 0 ? " active" : "");
      b.textContent = mk.name; b.dataset.mid = mk.mid;
      b.onclick = () => loadInv(mk.mid);
      tabs.appendChild(b);
    });
    loadInv(owned[0].mid);
  }).catch(() => {});
})();
// ══════════════════════════ TEAMS ══════════════════════════════════════════
(function renderTeams() {
  const teams = (TEAMS && TEAMS.teams) || [];
  const win = document.getElementById("teams-window");
  if (win && TEAMS && TEAMS.days) win.textContent = TEAMS.days;
  const tbody = document.getElementById("teams-tbody");
  const empty = document.getElementById("teams-empty");
  if (!tbody) return;
  if (!teams.length) { if (empty) empty.style.display = ""; return; }
  const totalCoins = teams.reduce((a, t) => a + (t.total || 0), 0);
  const stats = document.getElementById("stats-teams");
  if (stats) {
    stats.innerHTML = "";
    [["Teams", num(teams.length)], ["Tracked coins", num(totalCoins)], ["Window", (TEAMS.days || 7) + "d"]].forEach(([l, v]) => {
      const d = document.createElement("div"); d.className = "stat-card";
      d.innerHTML = `<div class="val">${v}</div><div class="lbl">${l}</div>`;
      stats.appendChild(d);
    });
  }
  tbody.innerHTML = "";
  teams.forEach((t, i) => {
    const medal = i === 0 ? "🥇" : i === 1 ? "🥈" : i === 2 ? "🥉" : (i + 1);
    const tw = (t.top_workers || []).map(w => `${esc(w.ign)} (${num(w.coins)})`).join(" · ");
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${medal}</td>
      <td class="item-name">${esc(t.captain)}${tw ? `<div style="font-size:10.5px;color:var(--muted)">${tw}</div>` : ""}</td>
      <td>${num(t.members)}</td>
      <td>${num(t.orders)} / ${num(t.order_coins)}c</td>
      <td>${num(t.sales_coins)}c</td>
      <td>${num(t.futures)}</td>
      <td class="mono" style="color:var(--accent)">${num(t.total)}c</td>`;
    tbody.appendChild(tr);
  });
})();


</script>
</body>
</html>"""



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
                          "pct": round(pct, 1), "owner": r.get("owner") or "", "price": price})
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
        if st in ("fulfilled", "cancelled"):
            continue
        mid = o.get("market_id") or "main"
        claimed = sum(int(c.get("qty") or 0) for c in (o.get("claims") or []))
        by_market.setdefault(mid, []).append({
            "id": int(o.get("id") or 0),
            "item": o.get("item") or "",
            "requested": int(o.get("requested") or 0),
            "claimed": claimed,
            "status": st or "open",
        })
    out = []
    for mid, orders in by_market.items():
        orders.sort(key=lambda x: x["id"], reverse=True)
        out.append({"market_id": mid, "name": names.get(mid, mid),
                    "orders": orders, "count": len(orders)})
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
    teams: dict = {}
    for r in rows:
        m = str(r["manager_id"]); k = r["kind"]
        c = float(r["coins"] or 0); q = int(r["qty"] or 0); wid = str(r["worker_id"])
        t = teams.setdefault(m, {"manager_id": m, "order_coins": 0.0, "sales_coins": 0.0,
                                 "orders": 0, "futures_qty": 0, "workers": {}})
        if k == "order":
            t["order_coins"] += c; t["orders"] += 1
        elif k == "sales":
            t["sales_coins"] += c
        elif k == "futures":
            t["futures_qty"] += q
        w = t["workers"].setdefault(wid, {"id": wid, "coins": 0.0})
        if k in ("order", "sales"):
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
    from datetime import datetime, timezone
    items         = _cached("items",         _load_items)
    markets       = _cached("markets",       _load_markets)
    earnings      = _cached("earnings",      _load_earnings)
    all_earnings  = _cached("all_earnings",  _load_all_earnings)
    market_prices = _cached("market_prices", _load_market_prices)
    stock_data    = _cached("stock_data",    _load_stock_data)
    teams_data    = _cached("teams_data",    _load_teams_data)
    inventory     = _cached("inventory",     _load_inventory_data)
    orders_board  = _cached("orders_board",  _load_orders_data)
    updated       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = (_PAGE
            .replace("__ITEMS_JSON__",    _jscript(items))
            .replace("__MARKETS_JSON__",  _jscript(_public_markets(markets)))
            .replace("__EARNINGS_JSON__", _jscript(earnings))
            .replace("__ALL_EARNINGS_JSON__", _jscript(all_earnings))
            .replace("__MARKET_PRICES_JSON__", _jscript(market_prices))
            .replace("__STOCKS_JSON__",   _jscript(stock_data))
            .replace("__TEAMS_JSON__",   _jscript(teams_data))
            .replace("__INVENTORY_JSON__", _jscript(inventory))
            .replace("__ORDERS_JSON__",   _jscript(orders_board))
            .replace("__UPDATED__",       updated))
    return web.Response(text=html, content_type="text/html")


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


async def _handle_api_earnings(request):
    return web.Response(
        text=json.dumps(_cached("earnings", _load_earnings), ensure_ascii=False),
        content_type="application/json",
    )


async def _handle_api_earnings_full(request):
    """Per-market earnings + per-item breakdown for the redesigned Earnings tab."""
    return web.Response(
        text=json.dumps(_cached("earnings_full", _load_earnings_full), ensure_ascii=False),
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
    anon = bool(_user_prefs().get(uid, {}).get("anonymous", False))
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
    try:
        html = m._render_full_report_html(
            f"Monthly Report — {mname}", mname, month_label,
            items, float(mo.get("income", 0) or 0), float(mo.get("spent", 0) or 0))
    except Exception as e:
        return web.Response(text=f"Could not render report: {e}", status=500,
                            content_type="text/plain")
    return web.Response(text=html, content_type="text/html")


async def _handle_health(request):
    return web.Response(text="ok")



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
        to_order, skipped_active, at_target = m._stock_refill_plan(mid, target)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    preview = [{"item": it, "qty": int(q)} for it, q, _ in to_order[:50]]
    if not bool(body.get("apply", False)):
        return web.json_response({"ok": True, "preview": True, "count": len(to_order),
                                  "skipped_active": skipped_active, "at_target": at_target,
                                  "items": preview})
    try:
        created = await m.run_on_bot_loop(m._create_restock_orders, to_order, mid)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    _CACHE.clear()
    return web.json_response({"ok": True, "created": int(created), "items": preview})


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
        to_order, skipped_active, at_target = m._stock_refill_plan(mid, item_targets=targets)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    preview = [{"item": it, "qty": int(q)} for it, q, _ in to_order[:50]]
    if not bool(body.get("apply", False)):
        return web.json_response({"ok": True, "preview": True, "count": len(to_order),
                                  "skipped_active": skipped_active, "at_target": at_target,
                                  "items": preview})
    if not to_order:
        return web.json_response({"ok": True, "created": 0, "items": []})
    try:
        created = await m.run_on_bot_loop(m._create_restock_orders, to_order, mid)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
    _CACHE.clear()
    return web.json_response({"ok": True, "created": int(created), "items": preview})


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
    text = str(body.get("items") or "")
    notes = str(body.get("notes") or "").strip()[:500]
    import Restocker_main as m, Restocker_db as _db
    parsed = m._parse_futures_bulk_text(text)
    if not parsed:
        return web.json_response({"ok": False,
                                  "error": "No items read — one per line, e.g. '2 barrels Warlord Potion'."})
    uid = str(sess.get("user_id") or "")
    uname = sess.get("name") or "Web owner"
    try:
        bulk_id = _db.create_futures_bulk(uid, uname, mid, uid, notes)
        for p in parsed:
            _db.add_futures_bulk_line(bulk_id, p["item"], p["qty"], p.get("unit", "pieces"),
                                      "", p.get("raw", ""))
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
    app.router.add_get("/",              _handle_index)
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
    app.router.add_get("/api/owner/loyalty",       _handle_owner_get_loyalty)
    app.router.add_post("/api/owner/set_loyalty",  _handle_owner_set_loyalty)
    app.router.add_post("/api/owner/generate_orders", _handle_owner_generate_orders)
    app.router.add_get("/api/owner/catalog",       _handle_owner_catalog)
    app.router.add_post("/api/owner/set_target",   _handle_owner_set_target)
    app.router.add_post("/api/owner/build_order",  _handle_owner_build_order)
    app.router.add_post("/api/owner/futures",      _handle_owner_futures)
    app.router.add_post("/api/order",    _handle_api_order)
    app.router.add_get("/api/network/orders", _handle_network_orders)
    app.router.add_post("/api/network/claim", _handle_network_claim)
    app.router.add_get("/report/{market}/{month}", _handle_report)
    app.router.add_get("/report/{market}",         _handle_report)
    app.router.add_get("/shares/{market}",         _handle_shares)
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
