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

    # ── Hive-ledger overlay: honey economics (value in, wages + owner cut out) live in
    # hive_ledger, not CSN (the hive shops buy at 0 coins) — merge them per market so the
    # money view is complete. Display-only; stock pricing reads hive_ledger separately.
    if db is not None:
        for mid, rows in list(result.items()):
            try:
                hl = db.get_hive_ledger_months(mid)
            except Exception:
                hl = {}
            if not hl:
                continue
            by_month = {r["month"]: r for r in rows}
            for mk, h in hl.items():
                r = by_month.get(mk)
                if r is None:
                    r = {"month": mk, "label": mk, "income": 0, "spent": 0, "net": 0, "items": {}}
                    rows.append(r)
                    by_month[mk] = r
                r["income"] += int(round(h["value"]))
                r["spent"]  += int(round(h["harvester_pay"] + h["owner_pay"]))
                r["net"]    += int(round(h["net"]))
                e = r["items"].setdefault("Hive harvest (honey value)",
                                          {"sold": 0, "bought": 0, "net": 0})
                e["net"] += int(round(h["net"]))
            rows.sort(key=lambda x: x["month"])

    # ── Company tabs: a parent stock + every market rolled into it appears as ONE
    # combined "<label> · all" ledger (listed first); members stay browsable on their
    # own tabs. Child months are weighted by their roll-up share (partner markets
    # contribute only the company's cut).
    if db is not None:
        groups: dict = {}
        for mid in list(result.keys()):
            try:
                parent = str(db.get_config(f"rollup_parent:{mid}") or "").strip()
            except Exception:
                parent = ""
            if not parent:
                continue
            try:
                share = float(db.get_config(f"rollup_share:{mid}") or 100.0) / 100.0
            except Exception:
                share = 1.0
            groups.setdefault(parent, []).append((mid, max(0.0, min(1.0, share))))
        combined_entries: dict = {}
        for parent, members in groups.items():
            label = ""
            try:
                label = str(db.get_config(f"stock_label:{parent}") or "").strip()
            except Exception:
                pass
            if not label:
                label = (markets.get(parent) or {}).get("name") or parent
            merged: dict = {}

            def _mix(rows, share, _m=merged):
                for r in rows:
                    t = _m.setdefault(r["month"], {"month": r["month"], "label": r["label"],
                                                   "income": 0, "spent": 0, "net": 0, "items": {}})
                    t["income"] += int(round(r["income"] * share))
                    t["spent"]  += int(round(r["spent"] * share))
                    t["net"]    += int(round(r["net"] * share))
                    for iname, iv in (r.get("items") or {}).items():
                        e = t["items"].setdefault(iname, {"sold": 0, "bought": 0, "net": 0})
                        e["sold"] += iv.get("sold", 0)
                        e["bought"] += iv.get("bought", 0)
                        e["net"] += int(round(iv.get("net", 0) * share))

            _mix(result.get(parent) or [], 1.0)
            for cmid, cshare in members:
                _mix(result.get(cmid) or [], cshare)
            if merged:
                key = f"{label} · all"
                if key in result or key in combined_entries:
                    key = f"{label} · all ({parent})"
                combined_entries[key] = sorted(merged.values(), key=lambda x: x["month"])
        if combined_entries:
            result = {**combined_entries, **result}

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



_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Abexilas Economy Hub — live market data</title>
<meta name="description" content="Live prices, market earnings, restock orders and the share exchange for the Abexilas server economy. Run by V Tech.">
<meta property="og:title" content="Abexilas Economy Hub">
<meta property="og:description" content="Live prices, market earnings, restock orders and the share exchange for the Abexilas server economy.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%2316a34a'/%3E%3Cpath d='M8 19v5h3v-5zM14.5 13v11h3V13zM21 8v16h3V8z' fill='%23fff'/%3E%3C/svg%3E">
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

    /* ── Dark card theme (the only theme) — the clean Cap Table Tracker layout in dark.
       A soft near-black surface set rather than the old brutalist #0A0A0A terminal, so
       the rounded cards / pills / sentence-case refinements below read as intended. ── */
    --bg:            #0d1117;
    --surface:       #161b22;
    --panel2:        #1c2230;
    --overlay:       #1c2230;
    --border:        #262c36;
    --border-dim:    #20252e;
    --border-strong: #333b47;

    --text:    #e6edf3;
    --text-body: #b6c0cc;
    --muted:   #8b949e;
    --faint:   #6e7681;

    --green:   #3fb950;
    --green-dim: #2ea043;
    --accent:  #3fb950;
    --red:     #f85149;
    --down:    #f85149;
    --amber:   #d29922;
    --gold:    #d29922;
    --yellow:  #d29922;
    --blue:    #58a6ff;
    --purple:  #bc8cff;
    --shadow:  0 1px 2px rgba(1,4,9,.5);

    --market-bnl:    #58a6ff;
    --market-nether: #ff7b39;
    --market-end:    #bc8cff;
    --market-sky:    #3fb950;
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
  /* ── Card refinements: rounded cards, pills, subtle shadows (Tracker look). Now the
     only theme, so applied unconditionally (was scoped to the light theme). ── */
  body { background-image: radial-gradient(var(--border) 0.5px, transparent 0.5px); }
  .chart-card,
  .chart-section,
  .table-wrap,
  .stat-card,
  .tick {
    border-radius: 12px; box-shadow: var(--shadow); background: var(--surface);
  }
  .stat-card { border: 1px solid var(--border); padding: 12px 16px; }
  .tab,
  .market-tab,
  .iv-tab,
  .auth-btn,
  .mini-btn { border-radius: 999px; }
  .auth-btn { color: #fff; }
  .auth-btn.ghost { color: var(--muted); }
  .logo-icon { color: #fff; border-radius: 8px; }
  .ownin,
  .own-price,
  .search-wrap input,
  textarea.ownin { border-radius: 8px; background: var(--panel2); border: 1px solid var(--border-strong); color: var(--text); }
  .tab.active,
  .market-tab.active { background: rgba(63,185,80,.14); }
  .ticker { border-radius: 12px; overflow: hidden; }
  /* Sentence-case microcopy (the Tracker look): buttons, nav, card titles and table
     headers. Small stat labels stay uppercase — a deliberate financial-dashboard
     convention, not a default. */
  .auth-btn { text-transform: none; letter-spacing: 0; font-size: 12px; }
  .nav-tab { text-transform: none; letter-spacing: .01em; font-size: 12.5px; }
  .chart-title { text-transform: none; letter-spacing: 0; font-size: 13.5px; font-weight: 600; color: var(--text); }
  th { text-transform: none; letter-spacing: .02em; font-size: 11px; }
  :focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
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
      <button class="auth-btn" id="iv-genorders" style="display:none">Generate restock orders (to 80%)</button>
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
      <div class="market-tabs" id="index-range" style="margin-bottom:8px"></div>
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
      <div class="market-tabs" id="stock-range" style="margin-bottom:8px"></div>
      <div class="chart-box"><canvas id="stock-chart"></canvas></div>
    </div>
    <div class="chart-card" id="ct-card" style="display:none">
      <div class="chart-title" id="ct-title">Cap table</div>
      <div class="stats" id="ct-stats"></div>
      <div id="ct-conc-wrap" style="margin:6px 0 14px">
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-bottom:6px">
          <span>Ownership concentration</span><span id="ct-conc-note"></span>
        </div>
        <div id="ct-conc" style="display:flex;height:12px;overflow:hidden;background:var(--panel2)"></div>
        <div id="ct-legend" style="display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:11.5px;color:var(--muted)"></div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Holder</th><th>Shares</th><th>%</th><th>Value</th></tr></thead>
          <tbody id="ct-tbody"></tbody>
        </table>
      </div>
      <div id="ct-note" style="font-size:11.5px;color:var(--faint);margin-top:10px"></div>
    </div>
    <div class="chart-card" id="inv-card" style="display:none">
      <div class="chart-title" id="inv-title">V Tech investors (GEX.PR)</div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Investor</th><th>Preferred shares</th><th>Share</th><th>Profit received</th></tr></thead>
          <tbody id="inv-tbody"></tbody>
        </table>
      </div>
      <div id="inv-note" style="font-size:11.5px;color:var(--faint);margin-top:10px"></div>
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
    <div id="bond-board" style="margin-top:28px;display:none">
      <div class="chart-title" style="margin-bottom:12px">Bond board — item-collateralized corporate debt</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Series</th><th>Issuer</th><th>Coupon</th><th>Matures</th>
              <th>Units left</th><th>Face sold</th><th>Item coverage</th><th>Status</th>
            </tr>
          </thead>
          <tbody id="bonds-tbody"></tbody>
        </table>
      </div>
      <div style="font-size:11.5px;color:var(--faint);margin-top:8px">
        House rule: items on record must cover ≥80% of outstanding face — coins don't count as bond collateral. Buy with <code>/bond buy</code> in Discord.
      </div>
    </div>
    <div id="holders-section" style="margin-top:28px;display:none">
      <div class="chart-title" style="margin-bottom:12px">Top holders · <span id="holders-market-name"></span>
        <a id="captable-link" href="#" target="_blank" style="float:right;font-size:13px;color:var(--accent);text-decoration:none">Full cap table →</a></div>
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
      <div class="chart-title">Place an order</div>
      <div id="or-place-locked" style="font-size:12.5px;color:var(--muted)">Log in (top right) to order — link your account with <code>/website_login</code> in Discord.</div>
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
    <div id="or-deliver" style="display:none;font-family:var(--font-data);font-size:12px;color:var(--muted);margin:0 0 12px">📍 Deliver to <span id="or-deliver-loc" style="color:var(--text)"></span></div>
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
      <div style="display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap">
        <button class="auth-btn" id="ob-build">Build order</button>
        <button class="auth-btn ghost" id="ob-futures" title="Same ticked items + targets, but sent as ONE futures request a manager approves (consignment terms)">Request as futures</button>
        <input id="fut-notes" class="ownin" placeholder="Futures notes (optional — enchants, delivery…)" style="flex:1;min-width:200px">
        <span id="ob-msg" style="font-size:12px;color:var(--muted)"></span>
      </div>
    </div>
    <div class="chart-card" id="fb-card" style="display:none">
      <div class="chart-title">Futures bills <span style="color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">— what you owe on consignment: you paid worker cost upfront, the margin comes due as you resell (tracked via your CSN sales)</span></div>
      <div id="fb-list"></div>
      <div id="fb-total" style="margin-top:10px;font-size:13px"></div>
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
    <div id="bulk-bar" style="display:none;margin-bottom:10px;padding:10px 14px;border:1px solid var(--border);border-radius:8px;background:var(--card);align-items:center;gap:12px">
      <span id="bulk-count" style="font-size:13px;color:var(--muted)">0 selected</span>
      <button class="mini-btn danger" id="bulk-remove">Remove selected</button>
      <button class="mini-btn" id="bulk-clear">Clear selection</button>
      <span id="bulk-msg" style="font-size:12px;color:var(--muted)"></span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th style="width:28px"></th><th>Item</th><th>Stock</th><th>Your price</th><th>Sold</th><th>Optimal</th><th>Actions</th></tr></thead>
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

// Theme: dark is the only theme (the light toggle was removed 2026-07-16). The dark
// palette lives in :root, so nothing to apply here at runtime.

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
// Real per-section routing — every subsection is its own URL (normal-website
// behavior: back button, shareable links). Remade sections get standalone pages
// (stocks → /exchange); the rest serve this shell with their tab activated.
const PAGE_ROUTES = {inventory: "/inventory", earnings: "/ledger", stocks: "/exchange",
                     orders: "/orders", teams: "/teams", mymarket: "/mymarket"};
const ROUTE_PAGES = {"/": "inventory", "/inventory": "inventory", "/ledger": "earnings",
                     "/orders": "orders", "/teams": "teams", "/mymarket": "mymarket"};
document.querySelectorAll(".nav-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    const dest = PAGE_ROUTES[tab.dataset.page];
    if (dest && dest !== window.location.pathname) { window.location.href = dest; return; }
  });
});
(function activateFromPath() {
  const page = ROUTE_PAGES[window.location.pathname] || "inventory";
  document.querySelectorAll(".nav-tab").forEach(t =>
    t.classList.toggle("active", t.dataset.page === page));
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  const el = document.getElementById("page-" + page);
  if (el) el.classList.add("active");
})();

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
      if (ownActive) hintEl.textContent = "You own this market — edit price or stock right here and press Enter to save (set stock to 0 to zero it, or use My Market to remove an item).";
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
        missingHtml = `<span class="badge neg-badge">missing ${num(missing)}</span>`;
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
    if (genMsg) genMsg.textContent = (res && res.ok) ? `Created ${res.created} order(s) — see the Orders tab.` : ((res && res.error) || "Failed.");
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

  const deliverEl = document.getElementById("or-deliver");
  const deliverLocEl = document.getElementById("or-deliver-loc");
  function render(){
    const loc = (DATA[active] || {}).sell_location || "";
    if (deliverEl) {
      if (loc) { deliverEl.style.display = ""; if (deliverLocEl) deliverLocEl.textContent = loc; }
      else { deliverEl.style.display = "none"; }
    }
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
    if(res&&res.ok){ subMsg.style.color="var(--green)"; subMsg.textContent=`Order #${res.order_id} sent — a manager will review it.`; cart=[]; notesIn.value=""; renderCart(); }
    else{ subMsg.style.color="var(--amber)"; subMsg.textContent=(res&&res.error)||"Failed."; }
  };
})();

// ══════════════════════════ STOCKS ═══════════════════════════════════════════
(function initStocks() {
  const markets = (STOCKS && STOCKS.markets) || [];

  // ── Abexilas Market Index ──
  // \u2500\u2500 chart time ranges: Hour / Day / Week / Month / Year / All \u2500\u2500
  const RANGES = [["1H","h"],["1D","d"],["1W","w"],["1M","mo"],["1Y","y"],["All","all"]];
  const RANGE_MS = { h: 3600e3, d: 86400e3, w: 604800e3, mo: 2592000e3, y: 31536000e3 };
  function tsOf(s) {
    if (!s) return NaN;
    const iso = (s.includes("Z") || s.includes("+")) ? s : s + "Z";   // logged_at is UTC
    return Date.parse(iso.replace(" ", "T"));
  }
  function rangeFilter(hist, range) {
    if (!hist) return [];
    if (range === "all") return hist;
    const cut = Date.now() - RANGE_MS[range];
    const out = hist.filter(p => { const t = tsOf(p.t); return !isNaN(t) && t >= cut; });
    return out.length > 1 ? out : hist.slice(-2);   // quiet range \u2192 show the last move, never a blank chart
  }
  function downsample(a, max) {
    max = max || 300;
    if (a.length <= max) return a;
    const s = Math.ceil(a.length / max);
    return a.filter((_, i) => i % s === 0 || i === a.length - 1);
  }
  function rangeLabel(t, range) {
    const s = (t || "").replace("T", " ");
    if (range === "y" || range === "all") return s.slice(0, 7);   // YYYY-MM
    if (range === "w" || range === "mo") return s.slice(5, 10);   // MM-DD
    return s.slice(5, 16);                                        // MM-DD HH:MM
  }
  function buildRangeButtons(el, current, onPick) {
    if (!el) return;
    el.innerHTML = "";
    RANGES.forEach(([lab, key]) => {
      const b = document.createElement("div");
      b.className = "market-tab" + (key === current ? " active" : "");
      b.textContent = lab;
      b.addEventListener("click", () => onPick(key));
      el.appendChild(b);
    });
  }

  let idxChart = null, idxRange = "d";
  function renderIndex() {
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
    buildRangeButtons(document.getElementById("index-range"), idxRange,
                      k => { idxRange = k; renderIndex(); });
    const ihist = downsample(rangeFilter(idx.history, idxRange));
    const ctx = document.getElementById("index-chart");
    if (idxChart) idxChart.destroy();
    const grad = ctx.getContext("2d").createLinearGradient(0, 0, 0, 220);
    grad.addColorStop(0, "rgba(34,255,122,.2)");
    grad.addColorStop(1, "rgba(34,255,122,0)");
    idxChart = new Chart(ctx, {
      type: "line",
      data: { labels: ihist.map(h => rangeLabel(h.t, idxRange)),
        datasets: [{ label: "Index", data: ihist.map(h => h.v),
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
  }
  renderIndex();

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
    d.innerHTML = `<div class="t-name">${esc(m.ticker)} · ${esc(m.name)} <span class="badge market-tag">${esc(m.rating||"C")}</span></div>
      <div class="t-price">${num(m.price.toFixed(2))} ¢</div>
      <div class="t-chg ${cls}">${arrow} ${m.pct.toFixed(2)}% · ${(m.backing_pct||0).toFixed(0)}% backed</div>`;
    ticker.appendChild(d);
  });

  // Stats
  const statsEl = document.getElementById("stats-stocks");
  const totalMcap = markets.reduce((s, m) => s + m.mcap, 0);
  const mover = markets.slice().sort((a, b) => Math.abs(b.pct) - Math.abs(a.pct))[0];
  const totalVisitors = markets.reduce((s,m)=>s+((m.quality&&m.quality.visitors_month)||0),0);
  [
    [markets.length,                       "Public Markets"],
    [num(Math.round(totalMcap)) + " ¢",    "Total Market Cap"],
    [num(Math.round(markets.reduce((s,m)=>s+(m.treasury||0),0))) + " ¢", "Total Treasury"],
    [num(totalVisitors),                   "Visitors / mo"],
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
      <td title="${m.quality ? `quality ${Math.round((m.quality.score||0)*100)}/100 · ${num(m.quality.visitors_month||0)} visits/mo · ${num(m.quality.order_value_30d||0)} ¢ orders filled/30d · ${m.quality.history_months||0} mo of reports` : "backing only"}"><span class="badge market-tag" style="margin-right:6px">${esc(m.rating||"C")}</span><span class="${(m.backing_pct||0) >= (m.backing_target||25) ? "up" : "down"}">${(m.backing_pct||0).toFixed(0)}%</span></td>
      <td>${m.holders_count}</td>`;
    tr.addEventListener("click", () => select(m.mid));
    tbody.appendChild(tr);
  });

  // ── Bond board ──
  const bonds = (STOCKS && STOCKS.bonds) || [];
  const bondSec = document.getElementById("bond-board");
  if (bondSec && bonds.length) {
    bondSec.style.display = "";
    const btb = document.getElementById("bonds-tbody");
    btb.innerHTML = "";
    bonds.forEach(b => {
      const covOk = (b.coverage||0) >= 80;
      const stIcon = b.status === "defaulted" ? "⚔️ defaulted" : (b.status === "active" ? "active" : "open — buyable");
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="item-name">${esc(b.name)} <span class="badge market-tag">#${b.id}</span></td>
        <td>${esc(b.market_id)}</td>
        <td>${b.coupon_pct.toFixed(2)}% / mo</td>
        <td>${esc(b.matures_at || "—")}</td>
        <td>${num(b.units_left)} @ ${num(Math.round(b.unit_price))} ¢</td>
        <td>${num(b.sold_face)} ¢</td>
        <td><span class="${covOk ? "up" : "down"}">${b.coverage != null ? b.coverage.toFixed(0) + "%" : "—"}</span></td>
        <td class="${b.status === "defaulted" || b.missed ? "down" : ""}">${stIcon}${b.missed ? " · " + b.missed + " missed" : ""}</td>`;
      btb.appendChild(tr);
    });
  }

  // Market selector tabs
  const tabsEl = document.getElementById("stock-market-tabs");
  markets.forEach((m, i) => {
    const b = document.createElement("button");
    b.className = "tab" + (i === 0 ? " active" : "");
    b.innerHTML = mktDot(m.mid) + esc(m.name);
    b.addEventListener("click", () => select(m.mid));
    tabsEl.appendChild(b);
  });

  // ── Cap table (live version of the pinned "GEX Cap Table Tracker") ─────────────────
  const CT_COLORS = ["#FF4444", "#F5A623", "#E8B339", "#B47FFF", "#4A9EFF", "#22FF7A"];
  async function loadCapTable(mid) {
    const card = document.getElementById("ct-card");
    if (!card) return;
    let r;
    try { r = await fetch("/api/exchange/captable?market_id=" + encodeURIComponent(mid)).then(x => x.json()); }
    catch (e) { r = null; }
    if (!r || !r.ok || !(r.rows || []).length) { card.style.display = "none"; return; }
    card.style.display = "";
    document.getElementById("ct-title").textContent =
      `Cap table — ${r.name}${r.ticker ? " (" + r.ticker + ")" : ""} · ${num(Math.round(r.outstanding))} shares outstanding · mark ${num(r.price.toFixed(2))} ¢`;
    // Stat tiles (Your stake / Your value highlighted like the tracker)
    const st = document.getElementById("ct-stats"); st.innerHTML = "";
    const tiles = [
      [num(Math.round(r.outstanding)), "Outstanding", 0],
      [r.logged_in ? `${num(Math.round(r.your_shares))} · ${r.your_pct}%` : "log in", "Your stake", 1],
      [r.logged_in ? num(r.your_value) + " ¢" : "—", "Your value (mark)", 1],
      [num(r.mktcap) + " ¢", "Total mktcap", 0],
      [String(r.holders), "Holders", 0],
      [`${num(Math.round(r.free_float))} · ${r.outstanding > 0 ? (100 * r.free_float / r.outstanding).toFixed(1) : 0}%`, "Free float", 0],
    ];
    tiles.forEach(([v, l, hot]) => {
      const d = document.createElement("div");
      d.className = "stat-card";
      if (hot && r.logged_in && r.your_shares > 0) d.style.cssText = "border:1px solid var(--accent);background:rgba(34,255,122,.05)";
      d.innerHTML = `<div class="val" style="font-size:16px">${v}</div><div class="lbl">${l}</div>`;
      st.appendChild(d);
    });
    // Concentration bar: top 5 + Others
    const conc = document.getElementById("ct-conc"), leg = document.getElementById("ct-legend");
    conc.innerHTML = ""; leg.innerHTML = "";
    const top5 = r.rows.slice(0, 5);
    const otherPct = Math.max(0, r.rows.slice(5).reduce((s, x) => s + x.pct, 0));
    const segs = top5.map((x, i) => [x.name, x.pct, CT_COLORS[i % CT_COLORS.length]]);
    if (otherPct > 0.01) segs.push(["Others", otherPct, "#555"]);
    segs.forEach(([nm, pct, col]) => {
      const s = document.createElement("div");
      s.style.cssText = `width:${Math.max(0.5, pct)}%;background:${col}`;
      s.title = `${nm} ${pct.toFixed(2)}%`;
      conc.appendChild(s);
      const li = document.createElement("span");
      li.innerHTML = `<span style="display:inline-block;width:8px;height:8px;background:${col};margin-right:5px"></span>${esc(nm)} ${pct.toFixed(2)}%`;
      leg.appendChild(li);
    });
    document.getElementById("ct-conc-note").textContent =
      `Top holder ${(r.rows[0] ? r.rows[0].pct : 0).toFixed(1)}% · Top 5 ${top5.reduce((s, x) => s + x.pct, 0).toFixed(1)}%`;
    // Holders table, "you" highlighted
    const tb = document.getElementById("ct-tbody"); tb.innerHTML = "";
    r.rows.forEach(x => {
      const tr = document.createElement("tr");
      if (x.you) tr.style.background = "rgba(34,255,122,.06)";
      tr.innerHTML = `<td style="color:var(--muted)">${x.rank}</td>` +
        `<td class="item-name">${esc(x.name)}${x.you ? ' <span class="badge" style="color:var(--accent);border:1px solid var(--accent);font-size:9px;padding:1px 6px">you</span>' : ""}</td>` +
        `<td>${num(Math.round(x.shares))}</td><td>${x.pct.toFixed(2)}%</td><td>${num(x.value)} ¢</td>`;
      tb.appendChild(tr);
    });
    document.getElementById("ct-note").textContent =
      "Values are notional at the current mark (share price) — realizing them moves the price."
      + (r.privileged ? "" : " Holder names follow each holder's privacy setting.");
  }

  // ── Investors card (GEX.PR register) — user-independent, loaded once ──
  (async function loadInvestors() {
    const card = document.getElementById("inv-card");
    if (!card) return;
    let r;
    try { r = await fetch("/api/investors").then(x => x.json()); }
    catch (e) { r = null; }
    const invs = (r && r.ok && r.investors) || [];
    if (!invs.length) { card.style.display = "none"; return; }
    card.style.display = "";
    const tb = document.getElementById("inv-tbody"); tb.innerHTML = "";
    invs.forEach(v => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="item-name">${esc(v.name)}</td>` +
        `<td>${num(Math.round(v.pref_shares))}</td>` +
        `<td>${v.share_pct.toFixed(1)}%</td>` +
        `<td>${num(Math.round(v.total_received))} ¢</td>`;
      tb.appendChild(tr);
    });
    document.getElementById("inv-note").textContent =
      `Investors receive ${r.pool_pct}% of each V Tech market's monthly net, split by share — paid automatically when monthly results record.`;
  })();

  let chart = null, stockRange = "d", curMid = null;
  function select(mid) {
    const m = markets.find(x => x.mid === mid);
    if (!m) return;
    curMid = mid;
    [...tabsEl.children].forEach(b => b.classList.toggle("active", b.textContent === m.name));
    loadCapTable(mid);
    document.getElementById("stock-chart-title").textContent = "Share price history — " + m.name;
    buildRangeButtons(document.getElementById("stock-range"), stockRange,
                      k => { stockRange = k; if (curMid) select(curMid); });
    const shist = downsample(rangeFilter(m.history, stockRange));
    const ctx = document.getElementById("stock-chart");
    if (chart) chart.destroy();
    const labels = shist.map(h => rangeLabel(h.t, stockRange));
    const cctx = ctx.getContext("2d");
    const grad = cctx.createLinearGradient(0, 0, 0, 300);
    grad.addColorStop(0, "rgba(34,255,122,.22)");
    grad.addColorStop(1, "rgba(34,255,122,0)");
    chart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets: [{
        label: "Price", data: shist.map(h => h.price),
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
      span.innerHTML = `<span class="auth-name">${esc(me.name || "You")}</span>`;
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
        const sg = prev.skipped_guard || [];
        const noPrice = sg.filter(s => s.reason === "no_price").length;
        const overCap = sg.filter(s => s.reason === "over_cap");
        if (!prev.count) {
          buildBtn.disabled = false;
          let m0 = "Nothing to restock — every ticked item is at or above its target.";
          if (sg.length) m0 += ` (${sg.length} item(s) skipped: ${noPrice} with no price, ${overCap.length} over the payout cap.)`;
          if (msgEl) msgEl.textContent = m0;
          return;
        }
        let warn = "";
        if (sg.length) {
          const parts = [];
          if (noPrice) parts.push(noPrice + " with no sell price");
          if (overCap.length) parts.push(overCap.length + " over the payout cap (" + overCap.slice(0,4).map(s => s.item).join(", ") + (overCap.length > 4 ? "..." : "") + ")");
          warn = "  --  " + sg.length + " item(s) skipped: " + parts.join("; ") + ". Set a price / order those manually if you meant to.";
        }
        if (!confirm(`Create ${prev.count} restock order(s) for your ticked items?` + warn)) {
          buildBtn.disabled = false; if (msgEl) msgEl.textContent = ""; return;
        }
        if (msgEl) msgEl.textContent = "Creating…";
        let res;
        try { res = await post("/api/owner/build_order", { market_id: mid, apply: true }); }
        catch (e) { res = { ok: false, error: "network" }; }
        buildBtn.disabled = false;
        if (msgEl) msgEl.textContent = (res && res.ok) ? `Created ${res.created} order(s) — see the Orders tab.` : ((res && res.error) || "Failed.");
      };
    }
    // "Request as futures": the SAME ticked items + targets, but submitted as ONE futures
    // request a manager approves (consignment terms) instead of direct restock orders.
    // Futures is for the high-margin goods (brews, enchanted gear, xp, dia) — blocks etc.
    // are auto-skipped so a mixed tick-list still does the right thing for each half.
    const futBtn = document.getElementById("ob-futures");
    if (futBtn) {
      const FUT_CATS = { "Brews": 1, "Enchanted Gear": 1, "Bows": 1 };
      const FUT_RX = /(diamond|\bxp\b|experience|bottle o)/i;
      const catOf = {};
      Object.keys(cats).forEach(c => (cats[c] || []).forEach(rw => { catOf[rw.item] = c; }));
      futBtn.onclick = async () => {
        futBtn.disabled = true; if (msgEl) msgEl.textContent = "Checking…";
        let prev;
        try { prev = await post("/api/owner/build_order", { market_id: mid, apply: false }); }
        catch (e) { prev = { ok: false, error: "network" }; }
        if (!prev || !prev.ok) { futBtn.disabled = false; if (msgEl) msgEl.textContent = (prev && prev.error) || "Failed."; return; }
        const all = prev.items || [];
        const fut = all.filter(l => FUT_CATS[catOf[l.item]] || FUT_RX.test(l.item));
        const skipped = all.length - fut.length;
        if (!fut.length) {
          futBtn.disabled = false;
          if (msgEl) msgEl.textContent = prev.count
            ? "No futures-worthy items under target — futures is for brews / enchanted gear / xp / dia."
            : "Nothing under target — tick items and set their % first.";
          return;
        }
        if (!confirm(`Request ${fut.length} item(s) as ONE futures order (manager approves)?`
                     + (skipped ? `\n${skipped} non-futures item(s) skipped — use Build order for those.` : ""))) {
          futBtn.disabled = false; if (msgEl) msgEl.textContent = ""; return;
        }
        if (msgEl) msgEl.textContent = "Submitting…";
        const notes = (document.getElementById("fut-notes") || { value: "" }).value.trim();
        let res;
        try { res = await post("/api/owner/futures", { market_id: mid, lines: fut, notes }); }
        catch (e) { res = { ok: false, error: "network" }; }
        futBtn.disabled = false;
        if (res && res.ok) {
          if (msgEl) msgEl.textContent = `Futures request #${res.bulk_id} sent (${res.count} item(s))`
            + (skipped ? ` · ${skipped} non-futures item(s) left for Build order.` : " — awaiting manager approval.");
          const nEl = document.getElementById("fut-notes"); if (nEl) nEl.value = "";
        } else {
          if (msgEl) msgEl.textContent = (res && res.error) || "Failed.";
        }
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
    // ── bulk-remove machinery: checkbox column, one confirm, NO page reload ──
    const bulkBar = document.getElementById("bulk-bar");
    const bulkCount = document.getElementById("bulk-count");
    const bulkMsg = document.getElementById("bulk-msg");
    function selectedBoxes() { return Array.from(tb.querySelectorAll("input.rm-sel:checked")); }
    function refreshBulkBar() {
      const n = selectedBoxes().length;
      bulkBar.style.display = n ? "flex" : "none";
      bulkCount.textContent = n + " selected";
      bulkMsg.textContent = "";
    }
    document.getElementById("bulk-clear").onclick = () => {
      tb.querySelectorAll("input.rm-sel").forEach(c => { c.checked = false; });
      refreshBulkBar();
    };
    document.getElementById("bulk-remove").onclick = async () => {
      const boxes = selectedBoxes();
      if (!boxes.length) return;
      if (!confirm(`Remove ${boxes.length} item(s) from this market?\n\nFull remove also adjusts historical net and your share price.`)) return;
      let done = 0, fail = 0;
      for (const box of boxes) {
        bulkMsg.textContent = `Removing ${done + fail + 1}/${boxes.length}…`;
        const res = await post("/api/owner/remove_item", { market_id: mid, item: box.dataset.item, mode: "full" });
        if (res && res.ok) {
          done++;
          const row = box.closest("tr");
          if (row) row.remove();
        } else { fail++; }
      }
      bulkMsg.textContent = `Removed ${done}` + (fail ? ` · ${fail} failed` : "") + " ✓";
      refreshBulkBar();
      if (done) bulkBar.style.display = "flex";   // keep the result message visible
    };
    Object.keys(groups).sort().forEach(cat => {
      const rows = groups[cat];
      const catId = "oc_" + cat.replace(/[^A-Za-z0-9]/g, "");
      const htr = document.createElement("tr");
      htr.style.cursor = "pointer"; htr.dataset.open = "0";
      const hsel = document.createElement("td");
      const selAll = document.createElement("input"); selAll.type = "checkbox";
      selAll.title = "Select every item in this category";
      selAll.onclick = (ev) => {
        ev.stopPropagation();
        tb.querySelectorAll('tr[data-cat="' + catId + '"] input.rm-sel').forEach(c => { c.checked = selAll.checked; });
        refreshBulkBar();
      };
      hsel.appendChild(selAll);
      const htd = document.createElement("td"); htd.colSpan = 6;
      htd.style.cssText = "color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-size:11px;padding:9px 0;user-select:none";
      htd.innerHTML = '<span class="oc-caret" style="display:inline-block;width:14px">▸</span>' + esc(cat) + " (" + rows.length + ")";
      htr.appendChild(hsel); htr.appendChild(htd);
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
        const tdSel = document.createElement("td");
        const selBox = document.createElement("input"); selBox.type = "checkbox"; selBox.className = "rm-sel";
        selBox.dataset.item = it.item;
        selBox.onclick = refreshBulkBar;
        tdSel.appendChild(selBox);
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
          rmBtn.textContent = "…";
          const res = await post("/api/owner/remove_item", { market_id: mid, item: it.item, mode: "full" });
          if (res && res.ok) { tr.remove(); refreshBulkBar(); }   // stay right here — no reload
          else { rmBtn.textContent = "Err"; setTimeout(() => { rmBtn.textContent = "Remove"; }, 1500); }
        };
        tdAct.appendChild(saveBtn); tdAct.appendChild(rmBtn);
        tr.appendChild(tdSel); tr.appendChild(tdName); tr.appendChild(tdStock); tr.appendChild(tdPrice); tr.appendChild(tdSold); tr.appendChild(tdOpt); tr.appendChild(tdAct);
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
  // (Futures is part of the Order Builder now — see the ob-futures handler inside
  // loadOrderBuilder: same ticked items + targets, sent as ONE futures request.)

  // ── Futures bills: the logged-in user's consignment debt, per deal, with per-line
  // resale progress. User-scoped (not per-market-tab), loaded once after login.
  async function loadFuturesBills() {
    const card = document.getElementById("fb-card");
    const list = document.getElementById("fb-list");
    const totalEl = document.getElementById("fb-total");
    if (!card || !list) return;
    let r;
    try { r = await fetch("/api/owner/futures_bills").then(x => x.json()); }
    catch (e) { r = { ok: false }; }
    const deals = (r && r.ok && r.deals) || [];
    if (!deals.length) { card.style.display = "none"; return; }
    card.style.display = "";
    list.innerHTML = "";
    let totRemaining = 0;
    deals.forEach(d => {
      totRemaining += (d.remaining || 0);
      const wrap = document.createElement("div");
      wrap.style.cssText = "border-bottom:1px solid var(--border);padding:8px 0";
      const head = document.createElement("div");
      head.style.cssText = "display:flex;gap:10px;align-items:center;font-size:12.5px;flex-wrap:wrap";
      const remCol = d.remaining > 0 ? "var(--down)" : "var(--accent)";
      const stTag = d.status === "pending" ? " · awaiting approval" : "";
      head.innerHTML =
        `<span style="color:var(--muted)">#${d.id}</span>` +
        `<span style="flex:1">${esc(d.market_id || "—")}${stTag}` +
        (d.unpriced ? ` · <span style="color:#E8B339">${d.unpriced} line(s) not priced yet</span>` : "") + `</span>` +
        `<span style="color:var(--muted)">upfront ${num(Math.round(d.upfront))}</span>` +
        `<span style="color:var(--muted)">owed ${num(Math.round(d.owed))} · paid ${num(Math.round(d.paid))}</span>` +
        `<span style="color:${remCol};font-weight:600">${num(Math.round(d.remaining))} ¢ left</span>`;
      wrap.appendChild(head);
      (d.lines || []).forEach(l => {
        if (!l.resold && !l.owed) return;      // only show lines with resale activity
        const ln = document.createElement("div");
        ln.style.cssText = "display:flex;gap:10px;padding:3px 0 3px 20px;font-size:12px;color:var(--muted)";
        ln.innerHTML = `<span style="flex:1">${esc(l.item)}</span>` +
                       `<span>resold ${num(l.resold)}/${num(l.qty)}</span>` +
                       `<span>→ ${num(Math.round(l.owed))} ¢</span>`;
        wrap.appendChild(ln);
      });
      list.appendChild(wrap);
    });
    if (totalEl) totalEl.innerHTML = totRemaining > 0
      ? `Total remaining: <b style="color:var(--down)">${num(Math.round(totRemaining))} ¢</b> — pay a manager; they record it with <code>/futures pay</code>.`
      : `Nothing outstanding — all caught up.`;
  }
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
    loadFuturesBills();   // user-scoped (their debt across all markets), so load once
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
    const medal = i + 1;
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
    teams: dict = {}
    for r in rows:
        m = str(r["manager_id"]); k = r["kind"]
        c = float(r["coins"] or 0); q = int(r["qty"] or 0); wid = str(r["worker_id"])
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
  <div class="rt"><div class="bp"><b class="mono" id="hWho">—</b><br><span id="hWhoSub">not linked</span></div></div>
</header>
<script>document.addEventListener('DOMContentLoaded',()=>{const p=location.pathname.replace('/','')||'inventory';
const a=document.querySelector('[data-nav="'+p+'"]');if(a)a.classList.add('on');
fetch('/api/me').then(r=>r.json()).then(me=>{if(me&&me.logged_in){
 document.getElementById('hWho').textContent=me.name||'linked';
 document.getElementById('hWhoSub').textContent='Discord linked';
 window.OWNERINFO=me;}}).catch(()=>{});});</script>
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
if(DATA.length>1){const _all=DATA.reduce((a,m)=>a.concat(m.items||[]),[]);
 DATA.unshift({market_id:"__all__",name:"All Markets",items:_all,count:_all.length,low:_all.filter(x=>x.capacity>0&&x.pct<=20).length});}
const CATORDER=["Wood & Logs","Ores & Minerals","Enchanted Gear","Redstone","Concrete & Clay","Nether","End","Ice & Snow","Farm & Food","Dyes & Wool","Mob Drops","Glass & Light","Nature","Building","Other"];
let act=0,sortK='pct',dir=1,catAct='All';
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
 return '<tr'+(((x.stock||0)<=0)?' class="zero"':'')+'><td>'+esc(x.item)+'</td>'+
  '<td class="num"><div class="fillcell"><div class="fillbar"><i style="width:'+p+'%;background:'+col(p)+'"></i></div>'+
  '<span class="pct" style="color:'+col(p)+'">'+Math.round(p)+'%</span></div></td>'+
  '<td class="num">'+fmt(x.stock)+'</td><td class="num">'+fmt(x.capacity)+'</td>'+
  '<td class="num">'+(x.price>0?(x.price<1?x.price.toFixed(2):fmt(x.price)):'—')+'</td></tr>';}
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
 rows.sort((a,b)=>{let x=a[sortK],y=b[sortK];
  if(typeof x==='string')return x.localeCompare(y)*dir;return ((x||0)-(y||0))*dir;});
 document.getElementById('empty').style.display=(DATA.length&&items.length)?'none':'';
 const html=rows.map(rowHTML).join('');
 document.getElementById('tb').innerHTML=html
  ||'<tr><td colspan="5" class="faint" style="height:34px">No items match.</td></tr>';}
document.getElementById('q').oninput=render;
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
</style></head><body>
__NAV__
<div class="content">
<div class="bar" id="chips"></div>
<div class="statrow" id="stats"></div>
<div class="chartwrap"><svg class="chart" id="chart" preserveAspectRatio="none"></svg><div class="tip" id="tip"></div></div>
<div class="cols">
<div class="panel"><div class="ph"><span class="t">Monthly ledger</span></div>
<table class="t"><thead><tr><th>Month</th><th>Income</th><th>Spent</th><th>Net</th></tr></thead><tbody id="mt"></tbody></table></div>
<div class="panel"><div class="ph"><span class="t">Top items · lifetime net</span></div>
<table class="t"><thead><tr><th>Item</th><th>Sold</th><th>Bought</th><th>Net ¢</th></tr></thead><tbody id="it"></tbody></table></div>
</div>
</div>
<script>
const EF=__EARNFULL_JSON__;
const fmt=n=>Math.round(n||0).toLocaleString('en-US').replace(/,/g,' ');
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const MS=(EF&&EF.markets)||[];let act=0;
function chips(){document.getElementById('chips').innerHTML=MS.map((m,i)=>
 '<div class="chip'+(i===act?' on':'')+'" data-i="'+i+'">'+esc(m.name||m.id)+' · '+(m.months||[]).length+' mo</div>').join('');
 document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{act=+c.dataset.i;chips();render();});}
function pathD(vals,w,h,pad){const mn=Math.min(...vals,0),mx=Math.max(...vals,1),rg=(mx-mn)||1;
 return vals.map((v,i)=>{const x=pad+i/((vals.length-1)||1)*(w-2*pad);const y=pad+(1-(v-mn)/rg)*(h-2*pad);
 return (i?'L':'M')+x.toFixed(1)+' '+y.toFixed(1);}).join(' ');}
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
 // chart
 const el=document.getElementById('chart');const w=el.clientWidth||900,h=220,pad=14;
 el.setAttribute('viewBox','0 0 '+w+' '+h);
 const v=nets.length>1?nets:[0,0];
 const col=(v[v.length-1]>=0)?css('--up'):css('--down');
 let grid='';for(let i=0;i<5;i++){const y=pad+i/4*(h-2*pad);
  grid+='<line x1="'+pad+'" y1="'+y+'" x2="'+(w-pad)+'" y2="'+y+'" stroke="'+css('--line')+'" stroke-width="1"/>';}
 const mn=Math.min(...v,0),mx=Math.max(...v,1),rg=(mx-mn)||1;
 const zy=pad+(1-(0-mn)/rg)*(h-2*pad);
 el.innerHTML=grid+'<line x1="'+pad+'" y1="'+zy+'" x2="'+(w-pad)+'" y2="'+zy+'" stroke="'+css('--line2')+'" stroke-width="1" stroke-dasharray="3 3"/>'+
  '<path d="'+pathD(v,w,h,pad)+'" fill="none" stroke="'+col+'" stroke-width="1.4"/>'+
  '<circle id="dot" r="3" fill="'+col+'" style="opacity:0"/>';
 const tip=document.getElementById('tip'),dot=document.getElementById('dot');
 el.onmousemove=e=>{const r=el.getBoundingClientRect();let i2=Math.round((e.clientX-r.left)/r.width*(v.length-1));
  i2=Math.max(0,Math.min(v.length-1,i2));const x=pad+i2/((v.length-1)||1)*(w-2*pad),y=pad+(1-(v[i2]-mn)/rg)*(h-2*pad);
  dot.setAttribute('cx',x);dot.setAttribute('cy',y);dot.style.opacity=1;
  tip.style.left=(x/w*100)+'%';tip.style.top=(y/h*100)+'%';tip.style.opacity=1;
  tip.textContent=(mo[i2]?mo[i2].label+': ':'')+fmt(v[i2])+' ¢';};
 el.onmouseleave=()=>{dot.style.opacity=0;tip.style.opacity=0;};
 // month table (newest first)
 document.getElementById('mt').innerHTML=mo.slice().reverse().map(x=>
  '<tr><td>'+esc(x.label||x.month)+'</td><td class="num">'+fmt(x.income)+'</td>'+
  '<td class="num">'+fmt(x.spent)+'</td><td class="num" style="color:'+((x.net||0)>=0?css('--up'):css('--down'))+'">'+fmt(x.net)+'</td></tr>').join('')
  ||'<tr><td colspan="4" class="faint" style="height:34px">No earnings recorded.</td></tr>';
 // lifetime items
 const agg={};mo.forEach(x=>(x.items||[]).forEach(it=>{const e=agg[it.item]=agg[it.item]||{s:0,b:0,n:0};
  e.s+=it.sold||0;e.b+=it.bought||0;e.n+=it.net||0;}));
 const rows=Object.entries(agg).sort((a,b)=>Math.abs(b[1].n)-Math.abs(a[1].n)).slice(0,30);
 document.getElementById('it').innerHTML=rows.map(([k,e])=>
  '<tr><td>'+esc(k)+'</td><td class="num">'+fmt(e.s)+'</td><td class="num">'+fmt(e.b)+'</td>'+
  '<td class="num" style="color:'+(e.n>=0?css('--up'):css('--down'))+'">'+fmt(e.n)+'</td></tr>').join('')
  ||'<tr><td colspan="4" class="faint" style="height:34px">No item data.</td></tr>';}
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
  <div id="locked" class="msg">Log in to order — run /website_login in Discord, then link on the old dashboard.</div>
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
</style></head><body>
__NAV__
<div class="content">
<div id="locked" class="locked">Owner tools — run <span class="mono" style="color:var(--ink)">/website_login</span> in Discord, link on the dashboard, then reload.</div>
<div id="panel" style="display:none">
<div class="bar" id="chips"></div>
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
      <div class="fld"><label>Qty added</label><input id="rq" type="number" min="1" style="width:90px"></div>
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
  document.getElementById('cat').innerHTML=names.sort().map(n=>'<option value="'+esc(n)+'">').join('');}catch(e){}}
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
  qty:+document.getElementById('rq').value||0,cost:+document.getElementById('rc').value||0}).catch(()=>({}));
 m.textContent=d.ok?'logged ✓':(d.error||'failed');};
document.getElementById('del').onclick=async()=>{const m=document.getElementById('dmsg');
 const it=document.getElementById('di').value.trim();if(!it){m.textContent='enter an item';return;}
 m.textContent='removing…';
 const d=await post('/api/owner/remove_item',{market_id:mid(),item:it,mode:'full'}).catch(()=>({}));
 m.textContent=d.ok?'removed ✓':(d.error||'failed');};
window.addEventListener('load',()=>{setTimeout(()=>{
 const me=window.OWNERINFO;
 if(me&&me.logged_in&&(me.owned||[]).length){owned=me.owned;
  document.getElementById('locked').style.display='none';
  document.getElementById('panel').style.display='';chips();loadMk();}
 },400);});
</script></body></html>"""


async def _handle_mymarket_page(request):
    html = (_MYMARKET_HTML.replace("__TERMINAL_CSS__", _TERMINAL_CSS)
            .replace("__NAV__", _TERMINAL_NAV))
    return web.Response(text=html, content_type="text/html")


# ── /exchange — pro-terminal exchange view (XTB/IBKR-style, read-only) ────────
# Purely additive page: consumes the existing /api/stocks and /api/me endpoints.
# The site can't trade (no per-user trade auth), so the order ticket is an
# ESTIMATOR that produces the exact /stock buy|sell command to run in Discord.
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
      <div class="cmd" id="cmd" title="Click to copy">/stock buy</div>
      <div class="hint">Trading runs through Discord — click the command to copy it.</div>
    </div>
    <div class="panel"><div class="ph"><span class="t">Your position</span></div><div class="pb" id="pos">
      <span class="faint">Link your Discord on the dashboard to see holdings.</span></div></div>
    <div class="panel"><div class="ph"><span class="t">Portfolio</span></div><div class="pb" id="pf">
      <span class="faint">—</span></div></div>
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
 document.getElementById('cmd').textContent='/stock '+side+' market_id:'+m.mid+' shares:'+Math.max(1,sh);}
document.getElementById('btnBuy').onclick=()=>{side='buy';
 document.getElementById('btnBuy').classList.add('on');document.getElementById('btnSell').classList.remove('on');calc();};
document.getElementById('btnSell').onclick=()=>{side='sell';
 document.getElementById('btnSell').classList.add('on');document.getElementById('btnBuy').classList.remove('on');calc();};
document.getElementById('amt').oninput=calc;
document.querySelectorAll('.chips button').forEach(b=>b.onclick=()=>{document.getElementById('amt').value=b.dataset.a;calc();});
document.getElementById('cmd').onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText(document.getElementById('cmd').textContent);
 const el=document.getElementById('cmd');const t=el.textContent;el.textContent='copied ✓';setTimeout(()=>{el.textContent=t;},700);};
document.querySelectorAll('#ranges button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('#ranges button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 rangeSec=+b.dataset.r;const m=MK.find(x=>x.mid===cur);if(m)drawMain(m);});
addEventListener('resize',()=>{const m=MK.find(x=>x.mid===cur);if(m)drawMain(m);});
async function boot(){
 try{const r=await fetch('/api/stocks');const d=await r.json();MK=d.markets||[];}catch(e){MK=[];}
 try{const r=await fetch('/api/me');ME=await r.json();
  if(ME.logged_in){document.getElementById('hWho').textContent=ME.name||'linked';
   document.getElementById('hWhoSub').textContent='Discord linked';}}catch(e){}
 if(MK.length){cur=MK[0].mid;renderList();render();}
 else{document.getElementById('list').innerHTML='<tr><td colspan="4" class="faint" style="height:40px;padding:0 10px">No public markets yet</td></tr>';}
 setInterval(async()=>{try{const r=await fetch('/api/stocks');const d=await r.json();
  MK=d.markets||MK;renderList();render();}catch(e){}},30000);}
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
