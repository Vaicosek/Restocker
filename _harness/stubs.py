"""Throwaway stubs so the REAL patched cogs/land_exchange.py can be imported and driven.

Nothing here is production code. The point is that the module under test is the actual
hotfix file, not a transcription of it — every assertion below exercises the shipped bytes.
"""
import sys, types, sqlite3, os, json, contextlib

# ── discord stub ──────────────────────────────────────────────────────────────
discord = types.ModuleType("discord")


class _Embed:
    def __init__(self, **kw):
        self.kw = kw
        self.fields = []

    def add_field(self, **kw):
        self.fields.append(kw)

    def set_footer(self, **kw):
        pass

    def set_image(self, **kw):
        pass

    def set_thumbnail(self, **kw):
        pass


class _ButtonStyle:
    primary = success = secondary = danger = 1


class _Item:
    def __init__(self, *a, **kw):
        pass


class _View:
    def __init__(self, *a, **kw):
        self.children = []

    def add_item(self, i):
        self.children.append(i)


class _DynamicItemMeta(type):
    def __getitem__(cls, _):
        return cls


class _DynamicItem(metaclass=_DynamicItemMeta):
    def __init_subclass__(cls, **kw):
        super().__init_subclass__()

    def __init__(self, *a, **kw):
        pass


ui = types.ModuleType("discord.ui")


class _Modal:
    def __init_subclass__(cls, **kw):
        super().__init_subclass__()

    def __init__(self, *a, **kw):
        pass

    def add_item(self, i):
        pass


ui.Modal, ui.View, ui.Button, ui.TextInput = _Modal, _View, _Item, _Item
ui.DynamicItem = _DynamicItem
ui.Item = _Item
discord.ui = ui
discord.Embed = _Embed
discord.ButtonStyle = _ButtonStyle
discord.Interaction = type("Interaction", (), {})
discord.Role = type("Role", (), {})
discord.Attachment = type("Attachment", (), {})
discord.Forbidden = type("Forbidden", (Exception,), {})
discord.File = type("File", (), {})

app_commands = types.ModuleType("discord.app_commands")


def _passthru_deco(*a, **kw):
    def deco(f):
        return f
    return deco


class _Group:
    def __init__(self, **kw):
        self.kw = kw

    def command(self, *a, **kw):
        return _passthru_deco()


class _ChoiceMeta(type):
    def __getitem__(cls, _):
        return cls


class _Choice(metaclass=_ChoiceMeta):
    def __init__(self, name=None, value=None):
        self.name, self.value = name, value


app_commands.Group = _Group
app_commands.Choice = _Choice
app_commands.command = _passthru_deco
app_commands.describe = _passthru_deco
app_commands.choices = _passthru_deco
app_commands.autocomplete = _passthru_deco
app_commands.checks = types.SimpleNamespace(has_permissions=_passthru_deco)
discord.app_commands = app_commands

ext = types.ModuleType("discord.ext")
commands_mod = types.ModuleType("discord.ext.commands")


class _Cog:
    @staticmethod
    def listener(*a, **kw):
        return _passthru_deco()


commands_mod.Cog = _Cog
tasks_mod = types.ModuleType("discord.ext.tasks")


class _Loop:
    """Keeps the coroutine so the harness can drive the REAL sweep body."""

    def __init__(self, coro):
        self.coro = coro
        self._running = False

    def __get__(self, obj, objtype=None):
        self._owner = obj
        return self

    def is_running(self):
        return self._running

    def start(self):
        self._running = True

    def cancel(self):
        self._running = False

    def before_loop(self, f):
        return f


def _loop(**kw):
    def deco(f):
        return _Loop(f)
    return deco


tasks_mod.loop = _loop
ext.commands, ext.tasks = commands_mod, tasks_mod

sys.modules["discord"] = discord
sys.modules["discord.ui"] = ui
sys.modules["discord.app_commands"] = app_commands
sys.modules["discord.ext"] = ext
sys.modules["discord.ext.commands"] = commands_mod
sys.modules["discord.ext.tasks"] = tasks_mod

# ── cogs.valuation stub ───────────────────────────────────────────────────────
cogs_pkg = types.ModuleType("cogs")
cogs_pkg.__path__ = [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cogs")]
sys.modules["cogs"] = cogs_pkg
val = types.ModuleType("cogs.valuation")
val.value_plot = lambda *a, **kw: {"assessed_value": 100000.0, "rate_per_chunk": 425000.0,
                                   "quality_multiplier": 1.0}
sys.modules["cogs.valuation"] = val


# ── Restocker_db stub: REAL DDL, REAL pragmas ─────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS balances (
    user_id TEXT PRIMARY KEY, coins REAL NOT NULL DEFAULT 0,
    principal REAL NOT NULL DEFAULT 0, lp REAL NOT NULL DEFAULT 0,
    updated_at TEXT);
CREATE TABLE IF NOT EXISTS coin_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, delta INTEGER NOT NULL,
    balance_after INTEGER NOT NULL, reason TEXT, ts TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS bot_config (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS land_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, seller_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'item', title TEXT, category TEXT, photos TEXT,
    market_id TEXT, land TEXT, chunks REAL NOT NULL DEFAULT 0, coords TEXT,
    description TEXT, image_url TEXT, winner_message TEXT,
    mode TEXT NOT NULL DEFAULT 'auction', quality TEXT NOT NULL DEFAULT 'raw',
    reserve REAL NOT NULL DEFAULT 0, buy_now REAL, current_bid REAL, current_bidder TEXT,
    min_increment_pct REAL NOT NULL DEFAULT 5.0, commission_pct REAL NOT NULL DEFAULT 5.0,
    listing_fee REAL NOT NULL DEFAULT 0,
    starts_at TEXT NOT NULL DEFAULT (datetime('now')), ends_at TEXT,
    anti_snipe_minutes INTEGER NOT NULL DEFAULT 5,
    status TEXT NOT NULL DEFAULT 'active', channel_id TEXT, message_id TEXT,
    sold_price REAL, sold_to TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')), closed_at TEXT);
CREATE TABLE IF NOT EXISTS land_bids (
    id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id INTEGER NOT NULL,
    bidder_id TEXT NOT NULL, amount REAL NOT NULL,
    ts TEXT NOT NULL DEFAULT (datetime('now')));
"""

_LAND_LISTING_FIELDS = (
    "seller_id", "kind", "title", "category", "photos", "market_id", "land", "chunks",
    "coords", "description", "image_url", "winner_message", "mode", "quality", "reserve",
    "buy_now", "current_bid", "current_bidder", "min_increment_pct", "commission_pct",
    "listing_fee", "starts_at", "ends_at", "anti_snipe_minutes", "status", "channel_id",
    "message_id", "sold_price", "sold_to", "closed_at")

rdb = types.ModuleType("Restocker_db")
rdb.DB_PATH = None
rdb._conn = None


def _conn():
    if rdb._conn is None:
        c = sqlite3.connect(rdb.DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")      # Restocker_db.py:24
        c.execute("PRAGMA busy_timeout=5000")     # Restocker_db.py:25
        c.execute("PRAGMA foreign_keys=ON")       # Restocker_db.py:26
        rdb._conn = c
    return rdb._conn


@contextlib.contextmanager
def db():
    conn = _conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


rdb.db = db

# Fault injection: a callable that may raise, consulted by the write helpers.
rdb.FAULT = None


def _maybe_fault(where, **kw):
    if rdb.FAULT:
        rdb.FAULT(where, **kw)


def get_balance(uid):
    with db() as c:
        r = c.execute("SELECT * FROM balances WHERE user_id=?", (str(uid),)).fetchone()
        return dict(r) if r else {"user_id": str(uid), "coins": 0, "principal": 0, "lp": 0}


def adjust_balance(uid, delta, *, counts_as_principal=True, reduce_principal=True):
    uid, d = str(uid), int(delta or 0)
    with db() as c:
        c.execute("INSERT INTO balances (user_id,coins,principal,lp) VALUES (?,0,0,0) "
                  "ON CONFLICT(user_id) DO NOTHING", (uid,))
        before = c.execute("SELECT coins FROM balances WHERE user_id=?", (uid,)).fetchone()
        old = int(before["coins"]) if before else 0
        if d > 0:
            c.execute("UPDATE balances SET coins=coins+?, principal=principal+? WHERE user_id=?",
                      (d, d if counts_as_principal else 0, uid))
        elif d < 0:
            amt = -d
            c.execute("UPDATE balances SET coins=MAX(0,coins-?) WHERE user_id=?", (amt, uid))
        row = c.execute("SELECT coins,principal FROM balances WHERE user_id=?", (uid,)).fetchone()
        coins = int(row["coins"])
    return coins, int(row["principal"]), coins - old


def record_coin_ledger(uid, delta, balance_after, reason=""):
    try:
        with db() as c:
            c.execute("INSERT INTO coin_ledger (user_id,delta,balance_after,reason) VALUES (?,?,?,?)",
                      (str(uid), int(delta), int(balance_after), (reason or "")[:200]))
    except Exception:
        pass


def coin_ledger_has(uid, reason):
    """Fails CLOSED — verbatim semantics of Restocker_db.py:1042."""
    try:
        with db() as c:
            return c.execute("SELECT 1 FROM coin_ledger WHERE user_id=? AND reason=? LIMIT 1",
                             (str(uid), str(reason))).fetchone() is not None
    except Exception:
        return True


def get_config(k, default=None):
    with db() as c:
        r = c.execute("SELECT value FROM bot_config WHERE key=?", (str(k),)).fetchone()
        return r["value"] if r else default


def set_config(k, v):
    with db() as c:
        c.execute("INSERT INTO bot_config (key,value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(k), str(v)))


def create_land_listing(**kw):
    cols = [k for k in kw if k in _LAND_LISTING_FIELDS]
    with db() as c:
        cur = c.execute(f"INSERT INTO land_listings ({','.join(cols)}) "
                        f"VALUES ({','.join('?' * len(cols))})", [kw[k] for k in cols])
        return int(cur.lastrowid)


def get_land_listing(lid):
    with db() as c:
        r = c.execute("SELECT * FROM land_listings WHERE id=?", (int(lid),)).fetchone()
        return dict(r) if r else None


def update_land_listing(lid, **kw):
    _maybe_fault("update_land_listing", listing_id=lid, **kw)
    cols = [k for k in kw if k in _LAND_LISTING_FIELDS]
    if not cols:
        return
    sc = ", ".join(f"{c}=?" for c in cols) + ", updated_at=datetime('now')"
    with db() as c:
        c.execute(f"UPDATE land_listings SET {sc} WHERE id=?", [kw[k] for k in cols] + [int(lid)])


def get_active_land_listings(mode=None):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM land_listings WHERE status='active' ORDER BY (ends_at IS NULL), ends_at"
        ).fetchall()]


def get_expired_active_listings():
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM land_listings WHERE status='active' AND ends_at IS NOT NULL "
            "AND ends_at <= datetime('now')").fetchall()]


def add_land_bid(lid, bidder, amount):
    with db() as c:
        return int(c.execute("INSERT INTO land_bids (listing_id,bidder_id,amount) VALUES (?,?,?)",
                             (int(lid), str(bidder), float(amount))).lastrowid)


def get_land_bids(lid, limit=20):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM land_bids WHERE listing_id=? ORDER BY ts DESC LIMIT ?",
            (int(lid), limit)).fetchall()]


def get_loyalty(uid):
    return {}


def add_loyalty_points(uid, pts):
    LOYALTY[str(uid)] = LOYALTY.get(str(uid), 0) + float(pts)


LOYALTY = {}
for _f in (get_balance, adjust_balance, record_coin_ledger, coin_ledger_has, get_config,
           set_config, create_land_listing, get_land_listing, update_land_listing,
           get_active_land_listings, get_expired_active_listings, add_land_bid,
           get_land_bids, get_loyalty, add_loyalty_points):
    setattr(rdb, _f.__name__, _f)
sys.modules["Restocker_db"] = rdb


# ── Restocker_main (core) stub ────────────────────────────────────────────────
main = types.ModuleType("Restocker_main")
HOUSE = {"coins": 0}


class _Log:
    def __init__(self):
        self.lines = []

    def _w(self, lvl, msg, *a):
        try:
            self.lines.append(f"{lvl} " + (msg % a if a else msg))
        except Exception:
            self.lines.append(f"{lvl} {msg} {a}")

    warning = lambda self, m, *a: self._w("WARN", m, *a)
    error = lambda self, m, *a: self._w("ERROR", m, *a)
    info = lambda self, m, *a: self._w("INFO", m, *a)
    exception = lambda self, m, *a: self._w("EXC", m, *a)


def add_coins(uid, amount, *, counts_as_principal=True, reason=""):
    amt = int(amount or 0)
    if amt == 0:
        cur = rdb.get_balance(str(uid))
        return int(cur.get("coins") or 0), 0
    coins, principal, applied = rdb.adjust_balance(uid, amt, counts_as_principal=counts_as_principal)
    rdb.record_coin_ledger(str(uid), applied, coins, reason)
    return coins, principal


def deduct_coins(uid, amount, *, reduce_principal=True, reason=""):
    amt = int(amount or 0)
    if amt <= 0:
        cur = rdb.get_balance(str(uid))
        return int(cur.get("coins") or 0), 0
    coins, principal, applied = rdb.adjust_balance(uid, -amt, reduce_principal=reduce_principal)
    rdb.record_coin_ledger(str(uid), applied, coins, reason)
    return coins, principal


def _credit_platform_balance(amount, *, market_id="", note="", month=None):
    amt = int(amount or 0)
    if amt <= 0:
        return 0
    HOUSE["coins"] += amt
    return amt


import datetime as _dt
main.add_coins = add_coins
main.deduct_coins = deduct_coins
main._credit_platform_balance = _credit_platform_balance
main.log = _Log()
main.bot = types.SimpleNamespace(get_cog=lambda n: None, add_dynamic_items=lambda c: None,
                                 is_ready=lambda: False, wait_until_ready=None,
                                 get_channel=lambda i: None, get_user=lambda i: None)
main.is_manager = lambda i: False
main._market_autocomplete = lambda *a, **kw: []
main._get_market = lambda m: None
main.any_item_autocomplete = lambda *a, **kw: []
main.utcnow_iso = lambda: _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
sys.modules["Restocker_main"] = main


def fresh_db(path):
    rdb.DB_PATH = path
    if rdb._conn is not None:
        try:
            rdb._conn.close()
        except Exception:
            pass
    rdb._conn = None
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(path + suf):
            os.remove(path + suf)
    with db() as c:
        c.executescript(DDL)
    HOUSE["coins"] = 0
    LOYALTY.clear()
    main.log.lines.clear()
    rdb.FAULT = None


def total_user_coins():
    with db() as c:
        r = c.execute("SELECT COALESCE(SUM(coins),0) s FROM balances").fetchone()
        return int(r["s"])


def ledger_count(uid, reason):
    with db() as c:
        return c.execute("SELECT COUNT(*) n FROM coin_ledger WHERE user_id=? AND reason=?",
                         (str(uid), str(reason))).fetchone()["n"]
