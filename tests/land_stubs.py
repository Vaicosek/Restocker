"""Throwaway stubs so the REAL land modules can be imported and driven in a test.

Nothing here is production code, and the list of what it fakes is deliberately
short. `Restocker_db`, `ledger_v2`, `ledger_migrate`, `land_escrow`, `land_settle`
and `cogs/land_exchange.py` are all the SHIPPED files — the assertions in
`test_land_settle.py` exercise the bytes that deploy, against a real SQLite
database with production pragmas and the real escrow triggers installed by the
real migration.

What is faked, and why each one is safe to fake:

  discord / discord.ext / discord.ui   The cog cannot be imported without them and
                                       no money decision is in them.
  cogs.valuation                       `value_plot()` returns a price suggestion
                                       that no settlement path reads.
  action_log                           The audit row is a side effect the
                                       settlement is explicitly not allowed to
                                       depend on ("it must never be able to fail a
                                       settlement that has already happened").
  Restocker_main                       Only its non-money surface is used by the
                                       cog at import time. `add_coins` and
                                       `deduct_coins` are wired to the REAL ones
                                       from the shipped module where the escrow
                                       tests need them, precisely so the
                                       `sqlite3.IntegrityError` narrowing can be
                                       proved rather than asserted.
"""
import contextlib
import os
import sqlite3
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def install() -> None:
    """Put the stubs on `sys.modules`. Idempotent."""
    if "discord" in sys.modules and getattr(sys.modules["discord"], "_land_stub", False):
        return
    _install_discord()
    _install_cogs_pkg()
    _install_action_log()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def _passthru(*_a, **_kw):
    def deco(f):
        return f
    return deco


def _install_discord() -> None:
    discord = types.ModuleType("discord")
    discord._land_stub = True

    class _Embed:
        def __init__(self, **kw):
            self.kw, self.fields = kw, []

        def add_field(self, **kw):
            self.fields.append(kw)

        def set_footer(self, **kw):
            pass

        def set_image(self, **kw):
            pass

        def set_thumbnail(self, **kw):
            pass

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

    class _Modal:
        def __init_subclass__(cls, **kw):
            super().__init_subclass__()

        def __init__(self, *a, **kw):
            pass

        def add_item(self, i):
            pass

    ui = types.ModuleType("discord.ui")
    ui.Modal, ui.View, ui.Button, ui.TextInput = _Modal, _View, _Item, _Item
    ui.DynamicItem, ui.Item = _DynamicItem, _Item
    discord.ui = ui
    discord.Embed = _Embed
    discord.ButtonStyle = type("ButtonStyle", (), {"primary": 1, "success": 1,
                                                   "secondary": 1, "danger": 1})
    for name in ("Interaction", "Role", "Attachment", "File", "Member", "Guild"):
        setattr(discord, name, type(name, (), {}))
    discord.Forbidden = type("Forbidden", (Exception,), {})
    discord.ChannelType = type("ChannelType", (), {"private_thread": 1, "public_thread": 2})
    discord.AllowedMentions = lambda **kw: kw

    app_commands = types.ModuleType("discord.app_commands")

    class _Group:
        def __init__(self, **kw):
            self.kw = kw

        def command(self, *a, **kw):
            return _passthru()

    class _ChoiceMeta(type):
        def __getitem__(cls, _):
            return cls

    class _Choice(metaclass=_ChoiceMeta):
        def __init__(self, name=None, value=None):
            self.name, self.value = name, value

    app_commands.Group, app_commands.Choice = _Group, _Choice
    for n in ("command", "describe", "choices", "autocomplete", "rename"):
        setattr(app_commands, n, _passthru)
    app_commands.checks = types.SimpleNamespace(has_permissions=_passthru)
    discord.app_commands = app_commands

    ext = types.ModuleType("discord.ext")
    commands_mod = types.ModuleType("discord.ext.commands")

    class _Cog:
        @staticmethod
        def listener(*a, **kw):
            return _passthru()

    commands_mod.Cog = _Cog
    tasks_mod = types.ModuleType("discord.ext.tasks")

    class _Loop:
        """Keeps the coroutine so a test can drive the REAL sweep body."""

        def __init__(self, coro):
            self.coro, self._running = coro, False

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

    tasks_mod.loop = lambda **kw: (lambda f: _Loop(f))
    ext.commands, ext.tasks = commands_mod, tasks_mod

    sys.modules.update({"discord": discord, "discord.ui": ui,
                        "discord.app_commands": app_commands, "discord.ext": ext,
                        "discord.ext.commands": commands_mod,
                        "discord.ext.tasks": tasks_mod})


def _install_cogs_pkg() -> None:
    cogs_pkg = types.ModuleType("cogs")
    cogs_pkg.__path__ = [str(ROOT / "cogs")]
    sys.modules["cogs"] = cogs_pkg
    val = types.ModuleType("cogs.valuation")
    val.value_plot = lambda *a, **kw: {"assessed_value": 100000.0,
                                       "rate_per_chunk": 425000.0,
                                       "quality_multiplier": 1.0}
    sys.modules["cogs.valuation"] = val


def _install_action_log() -> None:
    mod = types.ModuleType("action_log")
    mod.recorded = []
    mod.record = lambda *a, **kw: mod.recorded.append((a, kw))
    mod.ensure_schema = lambda: None
    sys.modules["action_log"] = mod


def install_core(real_money: bool = True) -> types.ModuleType:
    """A `Restocker_main` stand-in. `real_money` wires the SHIPPED add/deduct_coins.

    The point of `real_money` is the escrow-fallback proof: the test needs the
    actual `except sqlite3.IntegrityError: raise` clause from the shipped file,
    not a reimplementation of it, because the whole finding was that a
    reimplementation of that handler is exactly what went wrong.
    """
    core = types.ModuleType("Restocker_main")
    core.is_manager = lambda interaction: False
    core._market_autocomplete = _passthru
    core._get_market = lambda mid: {"market_id": mid}
    core.any_item_autocomplete = _passthru
    core.log = _Logger()
    core.bot = types.SimpleNamespace(add_dynamic_items=lambda *a, **k: None,
                                     is_ready=lambda: False)
    core.utcnow_iso = lambda: __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    core.platform_credits = []
    core._credit_platform_balance = lambda amount, **kw: core.platform_credits.append(
        (int(amount), kw))
    if real_money:
        add, deduct = _real_money_functions()
        core.add_coins, core.deduct_coins = add, deduct
    else:
        core.add_coins = core.deduct_coins = lambda *a, **kw: (0, 0)
    sys.modules["Restocker_main"] = core
    return core


def _real_money_functions():
    """Extract the SHIPPED `add_coins` / `deduct_coins` bodies and bind them.

    They live inside `Restocker_main.py`, which cannot be imported in a test (it
    builds a Discord client at module scope). So the two function definitions are
    compiled out of the real source and executed in a namespace carrying only
    what they reference. If the shipped file's narrowing of `except Exception`
    ever regresses, this stops passing — which is the point.
    """
    import ast
    src = (ROOT / "Restocker_main.py").read_text()
    tree = ast.parse(src)
    wanted = {"add_coins", "deduct_coins"}
    picked = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in wanted]
    if len(picked) != 2:
        raise AssertionError(f"expected add_coins and deduct_coins, found "
                             f"{[n.name for n in picked]}")
    module = ast.Module(body=picked, type_ignores=[])
    ns = {"sqlite3": sqlite3, "log": _Logger(),
          "_load_balances": lambda: {"users": {}},
          "_save_balances": _yaml_fallback_marker,
          "_get_user_bal": lambda users, uid: users.setdefault(
              str(uid), {"coins": 0, "principal": 0})}
    exec(compile(module, "<Restocker_main:money>", "exec"), ns)
    return ns["add_coins"], ns["deduct_coins"]


#: Set by `_yaml_fallback_marker` when the whole-table YAML rewrite is reached.
#: A test asserts this stays False across an escrow refusal: the fallback writing
#: even once means the trigger was defeated by the handler it constrains.
YAML_FALLBACK_HITS: list = []


def _yaml_fallback_marker(data):
    YAML_FALLBACK_HITS.append(data)


class _Logger:
    def __init__(self):
        self.lines = []

    def _rec(self, level, msg, *a):
        self.lines.append((level, str(msg) % a if a else str(msg)))

    def warning(self, msg, *a, **kw):
        self._rec("WARNING", msg, *a)

    def error(self, msg, *a, **kw):
        self._rec("ERROR", msg, *a)

    def info(self, msg, *a, **kw):
        self._rec("INFO", msg, *a)

    def debug(self, msg, *a, **kw):
        self._rec("DEBUG", msg, *a)

    def exception(self, msg, *a, **kw):
        self._rec("ERROR", msg, *a)


@contextlib.contextmanager
def fresh_db(tmpdir: str):
    """A real `restocker.db` with production pragmas and the escrow triggers in.

    Rule 7: the pragmas are the ones `Restocker_db` sets in production (`WAL`,
    `busy_timeout=5000`, `foreign_keys=ON`), because a past migration passed on a
    copy with `foreign_keys=0` and failed in production.
    """
    install()
    path = os.path.join(tmpdir, "restocker.db")
    import Restocker_db as db
    db.DB_PATH = Path(path)
    db._local = types.SimpleNamespace(conn=None)
    db.init_db()
    import ledger_migrate
    ledger_migrate.migrate(Path(path), verbose=False)
    import ledger_v2
    ledger_v2._local = types.SimpleNamespace(conn=None)
    try:
        yield db
    finally:
        for mod in (db, ledger_v2):
            conn = getattr(getattr(mod, "_local", None), "conn", None)
            if conn is not None:
                conn.close()
            mod._local = types.SimpleNamespace(conn=None)
