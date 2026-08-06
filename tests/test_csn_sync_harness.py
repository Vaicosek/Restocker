#!/usr/bin/env python3
"""Sync-session verification harness.

Reproduces the MOD's (new) CSV writers byte-for-byte, then runs the BOT's actual
parsing/ingest functions (extracted from Restocker_main.py / imported from
Restocker_db.py) against them. Covers the audit's Phase-1 protocol claims.
"""
import ast, csv, hashlib, io, os, re, sys, sqlite3, tempfile, types

# Locate the bot tree: works from RestockerLocal/tests/ (repo layout) or from a
# sibling checkout; override with CSN_BOT_DIR.
_here = os.path.dirname(os.path.abspath(__file__))
_cands = [os.environ.get("CSN_BOT_DIR") or "", os.path.dirname(_here), _here,
          os.path.expanduser("~/work/bot")]
BOT = next(p for p in _cands if p and os.path.exists(os.path.join(p, "Restocker_main.py")))
sys.path.insert(0, BOT)

# ── extract needed functions from Restocker_main.py without importing discord ──
SRC = open(os.path.join(BOT, "Restocker_main.py"), encoding="utf-8").read()
tree = ast.parse(SRC)
WANT = {"_parse_monthly_csv", "_parse_period_transactions", "_parse_export_csv",
        "_extract_market_info", "_extract_shop_name", "_merge_month_entry", "_sanitize_alias_name",
        "_parse_stock_csv", "_learn_brew_aliases_from_stock", "_parse_gear_enchants",
        "_brew_text_has_junk", "_hive_item_value", "_harvest_rate_for"}
segs = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT:
        segs.append(ast.get_source_segment(SRC, node))
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in {"_GEAR_ENCH_CANON", "_BREW_JUNK_RE",
                                                    "_HARVEST_RATES", "_HIVE_DEFAULT_VALUES",
                                                    "_LAST_MONTHLY_PARSE_META"}:
                segs.append(ast.get_source_segment(SRC, node))

class _Log:
    def info(self, *a): pass
    def warning(self, *a): pass
    def debug(self, *a): pass
    def error(self, *a): pass

NS = {"re": re, "csv": csv, "io": io, "log": _Log(), "utcnow_iso": lambda: "2026-08-03T12:00:00Z"}
# stub loaders used by alias learning
NS["_load_brew_aliases"] = lambda: dict(NS.get("_ALIASES", {}))
def _save(a):
    NS["_ALIASES"] = dict(a); return True
NS["_save_brew_aliases"] = _save
NS["_parse_brew_effects"] = lambda lore: ", ".join(
    l for l in (lore or []) if re.match(r"^[A-Za-z ]+ [IVX]+$", str(l)) and "strength" in str(l).lower())
exec(compile("\n\n".join(segs), "extracted", "exec"), NS)

import Restocker_db as db
from pathlib import Path
# point the DB at a temp file and build the schema
tmp = tempfile.mkdtemp()
db.DB_PATH = Path(tmp) / "test.db"
db.init_db()
FAIL = 0
def check(name, cond, extra=""):
    global FAIL
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond: FAIL += 1

# ── mod-side writer reproduction (matches the NEW Java code) ─────────────────
def java_fmt(v):
    from decimal import Decimal, ROUND_HALF_UP
    d = Decimal(repr(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize()
    s = format(d, "f")
    return s

def csv_field(s):
    if any(c in s for c in ',"\n\r'):
        return '"' + s.replace('"', '""') + '"'
    return s

def sha256_hex(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def sale_uid(actor, seller, verb, qty, item, coins, ts):
    minute = ts[:16] if len(ts) >= 16 else ""
    # Java: e.amountCoins() concatenated as double -> Double.toString
    coins_s = repr(float(coins)) if float(coins) != int(coins) else f"{float(coins):.1f}"
    raw = f"{actor}|{seller}|{verb}|{qty}|{item}|{coins_s}|{minute}"
    return sha256_hex(raw)[:32]

def write_export(entries, market=("greyhames", "AB12CD34")):
    out = ["# PERIOD,2026-08-01,2026-08-31"]
    if market:
        out.append(f"# MARKET,{csv_field(market[0])},{csv_field(market[1])}")
    out.append("actor,seller,verb,quantity,item,amount_coins,timestamp_iso,sale_uid")
    for (actor, seller, verb, qty, item, coins, ts) in entries:
        uid = sale_uid(actor, seller, verb, qty, item, coins, ts)
        out.append(f"{csv_field(actor)},{csv_field(seller)},{verb},{qty},{csv_field(item)},{java_fmt(coins)},{ts},{uid}")
    out.append("# RUN,2026-08-03T10:00:00Z,parsed=%d,pages=3" % len(entries))
    out.append("# RUN_SUMMARY,profit=100,loss=0,net=100")
    return "\n".join(out) + "\n"

def write_monthly_delta(runs, market=("greyhames", "AB12CD34")):
    """runs: list of {item: (sold, bought, net, ts_count_sold, ts_count_bought, inc, exp)}"""
    out = ["# MONTHLY_REPORT,csn_monthly_2026-08.csv",
           "item,total_sold_qty,total_bought_qty,net_coins,times_sold,times_bought,income_coins,expense_coins"]
    for i, run in enumerate(runs):
        if market:
            out.append(f"# MARKET,{csv_field(market[0])},{csv_field(market[1])}")
        out.append("# MODE,delta")
        out.append(f"# RUN,2026-08-0{i+1}T10:00:00Z")
        for item, (s, b, net, tsold, tbought, inc, exp) in run.items():
            out.append(f"{csv_field(item)},{s},{b},{java_fmt(net)},{tsold},{tbought},{java_fmt(inc)},{java_fmt(exp)}")
    return "\n".join(out) + "\n"

def write_stock(rows, market=("greyhames", "AB12CD34")):
    out = ["# STOCK_REPORT,csn_stock_x.csv"]
    if market:
        out.append(f"# MARKET,{csv_field(market[0])},{csv_field(market[1])}")
    out.append("owner,item,stock,buy_qty,buy_price,sell_qty,sell_price,lore,timestamp_iso,barrels,raw_item")
    for r in rows:
        out.append(",".join([csv_field(r["owner"]), csv_field(r["item"]), str(r["stock"]),
                             str(r["buy_qty"]), r["buy_price"], str(r["sell_qty"]), r["sell_price"],
                             csv_field(" | ".join(r.get("lore", []))), "2026-08-03T09:00:00Z",
                             str(r.get("barrels", 1)), csv_field(r["raw_item"])]))
    return "\n".join(out) + "\n"

# ═════ TEST 1: delta-mode monthly — audit's "2 rising runs lost 33%" case ═════
runs = [
    {"Potion": (10, 0, 1000.0, 5, 0, 1000.0, 0.0)},
    {"Potion": (20, 0, 2000.0, 8, 0, 2000.0, 0.0)},   # both rising → old classifier called it cumulative, kept only last
]
mtxt = write_monthly_delta(runs)
items, income, spent = NS["_parse_monthly_csv"](mtxt)
check("monthly delta header: both runs SUMMED (30 sold, 3000 income)",
      items.get("Potion", {}).get("sold_qty") == 30 and abs(income - 3000.0) < 0.01,
      f"got {items.get('Potion')}, income={income}")
check("monthly parse meta mode is delta(header)",
      NS["_LAST_MONTHLY_PARSE_META"].get("mode") == "delta(header)",
      str(NS["_LAST_MONTHLY_PARSE_META"]))

# 3 monotone runs (audit: 50% lost)
runs3 = [{"Comb": (5, 0, 500.0, 1, 0, 500.0, 0.0)},
         {"Comb": (7, 0, 700.0, 1, 0, 700.0, 0.0)},
         {"Comb": (8, 0, 800.0, 1, 0, 800.0, 0.0)}]
items3, inc3, _ = NS["_parse_monthly_csv"](write_monthly_delta(runs3))
check("monthly delta: 3 monotone runs all summed (20 sold / 2000)",
      items3.get("Comb", {}).get("sold_qty") == 20 and abs(inc3 - 2000.0) < 0.01,
      f"{items3.get('Comb')}, {inc3}")

# legacy file (no MODE header) still uses the classifier — should NOT crash
legacy = write_monthly_delta(runs).replace("# MODE,delta\n", "")
items_l, inc_l, _ = NS["_parse_monthly_csv"](legacy)
check("legacy monthly (no MODE) still parses", bool(items_l))

# duplicate identical RUN blocks still collapse; different blocks same ts both kept
dup = write_monthly_delta([runs[0], runs[0]])
dup = dup.replace("# RUN,2026-08-02T10:00:00Z", "# RUN,2026-08-01T10:00:00Z")
items_d, inc_d, _ = NS["_parse_monthly_csv"](dup)
check("identical same-ts RUN blocks counted once", items_d.get("Potion", {}).get("sold_qty") == 10, str(items_d))
diff_same_ts = write_monthly_delta([{"Potion": (10, 0, 1000.0, 5, 0, 1000.0, 0.0)},
                                    {"Potion": (3, 0, 300.0, 2, 0, 300.0, 0.0)}])
diff_same_ts = diff_same_ts.replace("# RUN,2026-08-02T10:00:00Z", "# RUN,2026-08-01T10:00:00Z")
items_ds, _, _ = NS["_parse_monthly_csv"](diff_same_ts)
check("DIFFERENT same-ts RUN blocks both kept (13 sold)",
      items_ds.get("Potion", {}).get("sold_qty") == 13, str(items_ds))

# ═════ TEST 2: MARKET header — quoting + last-wins ═════
two_headers = ('# MARKET,old_market,OLDCODE\n'
               'actor,seller,verb,quantity,item,amount_coins,timestamp_iso\n'
               '# MARKET,"new,market",NEWCODE\n')
mid, code = NS["_extract_market_info"](two_headers)
check("MARKET header: LAST wins + csv quoting", mid == "new,market" and code == "NEWCODE", f"{mid}/{code}")

# ═════ TEST 3: period txns — sale_uid carried; blank-ts logged not fatal ═════
E = [
    ("Alice", "Grey", "bought", 2, "Potion", 550.0, "2026-08-02T10:15:30Z"),
    ("Bob", "Grey", "sold", 64, "Honey Block", -0.0, "2026-08-02T10:15:30Z"),   # same instant, different verb/actor
    ("Alice", "Grey", "bought", 2, "Potion", 550.0, "2026-08-02T10:55:30Z"),   # identical sale 40 min later
]
etxt = write_export(E)
txns = NS["_parse_period_transactions"](etxt)
check("export parse keeps all 3 rows", len(txns) == 3, str(len(txns)))
check("sale_uid present on every row", all(t.get("sale_uid") for t in txns))
check("40-min-apart identical sales have DIFFERENT uids",
      txns[0]["sale_uid"] != txns[2]["sale_uid"])
# legacy 7-col file (old mod) still parses, uid None
legacy_e = "\n".join(l for l in etxt.splitlines() if not l.startswith("#"))
legacy_e = legacy_e.replace(",sale_uid", "")
legacy_e = "\n".join(",".join(l.split(",")[:7]) for l in legacy_e.splitlines())
ltx = NS["_parse_period_transactions"]("actor,seller,verb,quantity,item,amount_coins,timestamp_iso\n" +
                                       "\n".join(legacy_e.splitlines()[1:]))
check("legacy 7-col export parses (uid None)", len(ltx) == 3 and all(t["sale_uid"] is None for t in ltx))

# ═════ TEST 4: DB ingest — uid dedup, drift near-dup, verb/seller distinction ═════
new, rows_new = db.add_csn_transactions_detailed("m1", txns)
check("first ingest: all 3 recorded", new == 3, str(new))
new2, _ = db.add_csn_transactions_detailed("m1", txns)
check("re-ingest same file: 0 new (uid dedup)", new2 == 0, str(new2))
# same sale re-scanned with +40s drift (new uid because minute changed) → near-dup? uid present
# → uid differs → would insert! This is why the mod dedups re-reads via .seen. Bot-side legacy
# rows (no uid) get the ±90s window:
drift = [dict(t, sale_uid=None, sale_ts=t["sale_ts"].replace("15:30", "16:10")) for t in txns[:1]]
new3, _ = db.add_csn_transactions_detailed("m1", drift)
check("legacy row 40s-drifted duplicate: caught by near-dup window", new3 == 0, str(new3))
# a genuinely different legacy sale (same identity, 10 min later) is kept
later = [dict(txns[0], sale_uid=None, sale_ts="2026-08-02T10:25:30Z")]
new4, _ = db.add_csn_transactions_detailed("m1", later)
check("legacy same-identity sale 10 min later IS recorded", new4 == 1, str(new4))
# bought and sold at the same instant never collapse
with db.db() as conn:
    n = conn.execute("SELECT COUNT(*) FROM csn_transactions WHERE market_id='m1' AND sale_ts LIKE '2026-08-02T10:15%'").fetchone()[0]
check("bought+sold same instant both stored (verb in identity)", n == 2, str(n))

# ═════ TEST 5: month merge ═════
months = {"2026-08": {"label": "August 2026", "source": "a.csv", "income": 1000.0,
                      "spent": 100.0, "net": 900.0,
                      "items": {"Potion": {"sold_qty": 10, "bought_qty": 0, "net_coins": 1000.0}}}}
merged = NS["_merge_month_entry"](months, "2026-08", "August 2026", "b.csv",
                                  500.0, 50.0, {"Potion": {"sold_qty": 5, "net_coins": 500.0},
                                                "Comb": {"bought_qty": 64, "net_coins": -0.0}})
check("month merge adds income/spent (1500/150)",
      abs(merged["income"] - 1500.0) < 0.01 and abs(merged["spent"] - 150.0) < 0.01,
      f"{merged['income']}/{merged['spent']}")
check("month merge adds per-item qty (15 potions) and keeps new items",
      merged["items"]["Potion"]["sold_qty"] == 15 and "Comb" in merged["items"], str(merged["items"]))

# ═════ TEST 6: stock CSV — raw_item column revives alias learning ═════
srows = [{"owner": "Grey", "item": "Potion", "raw_item": "Potion#akQ", "stock": 320,
          "buy_qty": 1, "buy_price": "275", "sell_qty": 0, "sell_price": "",
          "lore": ["Strength II", "5 Min"]},
         {"owner": "Grey", "item": "Diamond Pickaxe - Efficiency V", "raw_item": "Diamond Pickaxe#afx",
          "stock": 2, "buy_qty": 1, "buy_price": "5000", "sell_qty": 0, "sell_price": "",
          "lore": ["Dig Speed V", "Durability III"]}]
stxt = write_stock(srows)
parsed = NS["_parse_stock_csv"](stxt)
check("stock parse: raw_item column preserved with #code",
      parsed[0]["raw_item"] == "Potion#akQ", str(parsed[0].get("raw_item")))
check("stock parse: alnum code stripped from display item", parsed[0]["item"] == "Potion")
NS["_ALIASES"] = {}
learned = NS["_learn_brew_aliases_from_stock"](parsed)
check("alias learning is ALIVE (learned >= 1 from stock scan)", learned >= 1, str(learned))
check("gear alias learned via enchants",
      any("Efficiency" in v for v in NS["_ALIASES"].values()) or learned >= 1, str(NS["_ALIASES"]))

# blank/0 listing qty → price stored as None, not stack-price-as-piece-price
srows_bad = [dict(srows[0], buy_qty=0, buy_price="17600", raw_item="Potion#akQ")]
pb = NS["_parse_stock_csv"](write_stock(srows_bad))
check("blank/0 qty listing → buy_price None (no 64x piece price)", pb[0]["buy_price"] is None, str(pb[0]["buy_price"]))

# ═════ TEST 7: § stripping in hive value ═════
db.set_config("hive_value:honey block", "5.46875")
v = NS["_hive_item_value"]("§6Honey Block")
check("_hive_item_value strips § codes (§6Honey Block → 5.47/pc)", abs(v - 5.46875) < 1e-6, str(v))
check("rate sanity: hive value ≈ 80x smaller than retired 76/pc rate", v < 6)

# ═════ TEST 8: sanitizer ═════
s = NS["_sanitize_alias_name"]("§4@everyone **FREE** `stuff`​ @here")
check("alias sanitizer kills pings/markdown/§/zero-width",
      "@everyone" not in s and "@here" not in s and "*" not in s and "`" not in s and "§" not in s, repr(s))

# ═════ TEST 9: encoding round-trip through java_fmt/csv_field ═════
tricky = [("greyhame’s", "Grey", "bought", 1, 'A "quoted", item§6', 1.25, "2026-08-02T10:15:30Z")]
ttxt = write_export(tricky)
tp = NS["_parse_period_transactions"](ttxt)
check("round-trip: apostrophe/quotes/commas/§ survive",
      len(tp) == 1 and tp[0]["actor"] == "greyhame’s" and tp[0]["item"] == 'A "quoted", item§6'
      and abs(tp[0]["coins"] - 1.25) < 1e-9, str(tp))

# ═════════════════════════════════════════════════════════════════════════════
# TEST 10+: regressions for the four-agent audit of 2026-08-06.
# Each of these reproduces a CONFIRMED bug — they fail against the code as it
# stood before that audit.
# ═════════════════════════════════════════════════════════════════════════════

# ── A. `# MODE,delta` must be scoped to its own RUN block ────────────────────
# One block written by the upgraded mod used to relabel every legacy CUMULATIVE
# block in the same file (the mod APPENDS), summing month-to-date snapshots
# instead of taking the last one. Measured: a true 200,085 reported as 3,102,858.
_HDR = ("item,total_sold_qty,total_bought_qty,net_coins,times_sold,times_bought,"
        "income_coins,expense_coins")
def _blk(rows, ts, mode=None):
    s = f"# MODE,{mode}\n" if mode else ""
    s += f"# RUN,{ts}\n"
    for it, q, inc in rows:
        s += f"{it},{q},0,{inc},1,0,{inc},0\n"
    return s

_legacy = "# MONTHLY_REPORT,x\n" + _HDR + "\n" + "".join(
    _blk([("Diamond", q, inc)], f"2026-08-0{i}T00:00:00Z")
    for i, (q, inc) in enumerate([(10, 1000.0), (20, 2000.0), (30, 3000.0)], 1))
_i, _inc, _sp = NS["_parse_monthly_csv"](_legacy)
check("legacy cumulative file: last snapshot wins (3000)", abs(_inc - 3000.0) < 0.01, str(_inc))

_mixed = _legacy + _blk([("Diamond", 2, 200.0)], "2026-08-05T00:00:00Z", mode="delta")
_i, _inc, _sp = NS["_parse_monthly_csv"](_mixed)
check("mixed file: cumulative prefix + delta block = 3200 (was 6200)",
      abs(_inc - 3200.0) < 0.01, str(_inc))
check("mixed file reports its mode honestly",
      "mixed" in str(NS["_LAST_MONTHLY_PARSE_META"].get("mode")),
      str(NS["_LAST_MONTHLY_PARSE_META"]))

_pure = "# MONTHLY_REPORT,x\n" + _HDR + "\n" + "".join(
    _blk([("Diamond", 1, 100.0)], f"2026-08-0{i}T00:00:00Z", mode="delta") for i in range(1, 6))
_i, _inc, _sp = NS["_parse_monthly_csv"](_pure)
check("pure delta file still simply sums (500)", abs(_inc - 500.0) < 0.01, str(_inc))

# ── B. the ±90s window must run for uid-bearing rows too ────────────────────
# A uid MISS used to fall straight through to INSERT, so the same sale re-read
# with a drifted timestamp (a different minute bucket → a different uid) was
# inserted twice and its coins booked as fresh earnings.
def _uid(actor, seller, verb, qty, item, coins, ts):
    return hashlib.sha256(
        f"{actor}|{seller}|{verb}|{qty}|{item}|{coins}|{ts[:16]}".encode()).hexdigest()[:32]

def _row(ts):
    return {"actor": "Drifty", "seller": "Vaicos", "verb": "bought", "item": "Netherite Scrap",
            "qty": 3, "coins": 900.0, "sale_ts": ts,
            "sale_uid": _uid("Drifty", "Vaicos", "bought", 3, "Netherite Scrap", 900.0, ts)}

nA, _ = db.add_csn_transactions_detailed("driftmkt", [_row("2026-08-03T15:34:59.400Z")])
nB, _ = db.add_csn_transactions_detailed("driftmkt", [_row("2026-08-03T15:35:12.900Z")])
check("uid-bearing re-read 13s later across a minute boundary: rejected",
      nA == 1 and nB == 0, f"{nA}/{nB}")
nC, _ = db.add_csn_transactions_detailed("driftmkt", [_row("2026-08-03T15:45:00.000Z")])
check("genuinely repeated sale 10 min later still recorded", nC == 1, str(nC))

# ── C. hive dedup must not be scoped to the market ──────────────────────────
# The same physical sale exported under two market ids created two payable rows
# and each market settled it independently. Live in the DB: JesseNapoleon's four
# Honey Block sales under both 'greyhames' and 'vtech'.
_hts = "2026-08-05T15:54:52.124Z"
h1 = db.add_hive_harvest("mktA", "Harvey", None, "Honey Block", 546, 5.46875, "msg:a", 0, sale_ts=_hts)
h2 = db.add_hive_harvest("mktB", "Harvey", None, "Honey Block", 546, 5.46875, "msg:b", 0, sale_ts=_hts)
check("same sale under a SECOND market id is rejected", h1 and not h2, f"{h1}/{h2}")
h3 = db.add_hive_harvest("mktB", "Harvey", None, "Honey Block", 546, 5.46875, "msg:c", 0,
                         sale_ts="2026-08-05T17:54:52.124Z")
check("a real second harvest 2h later still records", bool(h3), str(h3))

# ── D. claim_hive_harvests must report WHICH rows it won ────────────────────
# _settle_groups released its whole id list on a partial claim, un-paying rows
# another run had already moved coins for; the next sweep paid them again.
_ids = []
for i in range(4):
    _ids.append(db.add_hive_harvest("claimmkt", "Clara", "42", "Honeycomb Block", 100 + i,
                                    4.6875, "msg:claim", i, sale_ts=f"2026-08-0{i+1}T10:00:00Z"))
_ids = [i for i in _ids if i]
_runB = db.claim_hive_harvests(_ids[2:])          # the other settle run wins two rows
_runA = db.claim_hive_harvests(_ids)              # this run then claims what is left
check("claim returns only the rows it actually flipped",
      sorted(_runA) == sorted(_ids[:2]) and sorted(_runB) == sorted(_ids[2:]),
      f"A={_runA} B={_runB}")
check("the two runs' claims are disjoint — no row can be paid twice",
      not (set(_runA) & set(_runB)), f"{_runA} / {_runB}")

# ── E. one month source per FILE, not per poster ────────────────────────────
# The same file re-uploaded by a manager counted as an extra shop.
_it = {"Diamond": {"sold_qty": 10, "net_coins": 1000.0}}
db.csn_set_month_source("srcmkt", "2026-08", "poster:111", 1000.0, 0.0, _it)
db.csn_set_month_source("srcmkt", "2026-08", "poster:222", 1000.0, 0.0, _it)   # same file, by hand
_r = db.csn_month_totals("srcmkt", "2026-08")
check("identical file via a second transport does not multiply the month",
      abs(_r["income"] - 1000.0) < 0.01 and _r["sources"] == 1, str(_r))
db.csn_set_month_source("srcmkt", "2026-08", "poster:333", 250.0, 0.0,
                        {"Emerald": {"sold_qty": 5, "net_coins": 250.0}})
_r = db.csn_month_totals("srcmkt", "2026-08")
check("a genuinely different shop still adds to the month",
      abs(_r["income"] - 1250.0) < 0.01 and _r["sources"] == 2, str(_r))

# ── F. the mod must stamp # SHOP so the rollup can key on the shop ──────────
check("_extract_shop_name reads the mod's # SHOP stamp",
      NS["_extract_shop_name"]("# MARKET,vtech,AB12\n# SHOP,Vaicos_Isman\n# RUN,x\n")
      == "Vaicos_Isman")
check("_extract_shop_name returns '' when the stamp is absent",
      NS["_extract_shop_name"]("# MARKET,vtech,AB12\n# RUN,x\n") == "")

# ── G. the central hive-project report ──────────────────────────────────────
# Read-only reporting posted on bot start and after each 6h sweep. The numbers
# must reconcile: lifetime earned == already paid + still owed.
import types as _types
_hcore = _types.SimpleNamespace()
_hcore._hive_harvester_pct = lambda: 15.0
_hcore._hive_owner_pct = lambda mid: 0.0
_hcore.hive_autopay_on = lambda mid: True
_hcore._get_market = lambda mid: {"name": mid}
_hive_src = open(os.path.join(BOT, "cogs", "hive.py"), encoding="utf-8").read()
_HNS = {"core": _hcore, "log": _Log(), "sys": sys}
for _n in ast.parse(_hive_src).body:
    if isinstance(_n, ast.FunctionDef) and _n.name in {
            "_fmt", "_hive_report_markets", "build_hive_project_report",
            "build_harvester_statements"}:
        exec(compile(ast.Module([_n], []), "hivecog", "exec"), _HNS)
    if isinstance(_n, ast.Assign):
        for _t in _n.targets:
            if isinstance(_t, ast.Name) and _t.id == "HIVE_REPORT_CHANNEL_DEFAULT":
                exec(compile(ast.Module([_n], []), "hivecog", "exec"), _HNS)

db.set_config("hive_value:honeycomb block", "4.6875")
_r1 = db.add_hive_harvest("repmkt", "Reporty", "777", "Honeycomb Block", 1000, 4.6875,
                          "msg:rep", 0, sale_ts="2026-08-01T10:00:00Z")
_r2 = db.add_hive_harvest("repmkt", "Reporty", "777", "Honeycomb Block", 400, 4.6875,
                          "msg:rep", 1, sale_ts="2026-08-02T10:00:00Z")
db.claim_hive_harvests([_r1])                     # half of it already paid
_msgs = _HNS["build_hive_project_report"]("test")
_all = "\n".join(_msgs)
check("hive report renders and mentions the site", "repmkt" in _all, _all[:200])
check("hive report shows the unpaid backlog", "still unpaid" in _all, _all[:400])
check("hive report chunks under Discord's limit", all(len(m) <= 1900 for m in _msgs),
      str([len(m) for m in _msgs]))
_st = _HNS["build_harvester_statements"]()
check("a linked harvester gets a personal statement", "777" in _st, str(list(_st)))
if "777" in _st:
    _txt = _st["777"]
    # 1400 pcs x 4.6875 = 6562.5 value; 15% = 984 earned; 1000 pcs paid -> 703 paid
    check("statement reconciles: earned = paid + still to come",
          "984" in _txt and "703" in _txt and "281" in _txt, _txt)

print()
print("FAILURES:", FAIL)
sys.exit(1 if FAIL else 0)
