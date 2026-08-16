"""cogs/panel_skus.py — `/go`, the one command that replaces typing ids.

ONE new slash command, and it is the only one in this change. It earns its slot
because it *removes* typing from every other surface: instead of
`/my market market_id:greyhames`, `/hive settings location:Spawn Hive 3`,
`/lot view id:412`, a user reads four characters off the panel they are looking
at and says them out loud. Support types `/go k7rq` and lands on the same panel,
on the same entity, with the same permissions applied by the same code.

HOW IT OPENS A PANEL
--------------------
It does not reimplement any panel. Two strategies, in order:

  1. an opener a cog registered explicitly (`panel_skus.register_opener`);
  2. otherwise it delegates to the panel's *existing* slash command through the
     bot's command tree, passing the entity as that command's own parameter.

Strategy 2 is why this file does not import `views.*`: permission checks,
rendering and every future change to a panel stay in exactly one place. If a
panel command is renamed, the fix is one line in ADAPTERS below and nothing else.

Delegation runs the target's `_check_can_run` first. Calling `cmd.callback(...)`
is calling the raw function, which skips every `@app_commands.checks.*` decorator
on it — so without that line `/go <code>` would be a way round any panel command
that guards itself with a decorator rather than in its body.

DISCORD RULES OBSERVED
----------------------
* `code` is a slash-command STRING PARAMETER with autocomplete — a modal could
  not autocomplete it and a view could not hold free text.
* No defer here: the delegated panel command owns the first response (all of
  them answer well inside 3s). We only respond ourselves when we are not
  delegating, and we always check `response.is_done()` first.
* The disambiguation picker is a select, is ephemeral, and re-resolves the token
  from its own value on click — it holds no state that must survive a restart.
"""
from __future__ import annotations

import sys

import discord
from discord import app_commands
from discord.ext import commands

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
log = getattr(core, "log", None)

import panel_skus  # noqa: E402  (module lives beside Restocker_main.py, same as Restocker_db)


# ── Where each panel already lives ──────────────────────────────────────────
# `command` is a LIST of qualified names to try, in order — the first one that is
# actually registered in this build wins. `param` is that command's own entity
# parameter. This table is the whole integration surface: nothing else in this
# change knows about views.* or about any panel's internals.
#
# This table only carries the panel keys that STAMP_TARGETS below can actually
# MINT an address for. It used to carry all nine of `panel_skus.PANELS`, which
# advertised `/go` support for five panels whose embed builder does not exist in
# this bot — nothing could ever stamp them, so no token could ever be minted for
# them, so no `/go` code could ever resolve to them. Advertising a route to a
# panel that cannot be addressed is the same lie as a Rollback button with no
# producer. A panel that later grows a real builder gets a STAMP_TARGETS entry
# and a row here, in the same edit.
#
#   (confirmed) my market       cogs/market.py:268-272        param market_id
#   (confirmed) realestate info cogs/land_exchange.py:1173-6  param listing_id (int)
#   (unstaged)  stock / team panel commands live in cogs this checkout does not
#               carry. Their builders ARE confirmed (Restocker_main.py:12931 and
#               :3240), so the address mints; if the command turns out to be
#               registered under a different name, `_open` says exactly that
#               instead of pretending.
ADAPTERS: dict[str, dict] = {
    "market":   {"command": ["my market", "market settings"],   "param": "market_id"},
    "lot":      {"command": ["realestate info", "lot view"],    "param": "listing_id"},
    "stock":    {"command": ["stock panel", "stock"],           "param": "market_id"},
    "team":     {"command": ["team settings", "team panel"],    "param": "manager"},
}

# ── Panels whose embed builder we stamp automatically ───────────────────────
# Each entry is (panel_key, module_path, function_name, extractor, evidence).
# Wrapping the builder is what lets a panel print its own address without editing
# a single line inside an 18k-line module.
#
# `MAIN` resolves to whichever module object Restocker_main actually is at runtime
# (it runs as `__main__`, so `import Restocker_main` would give you a SECOND copy
# of the module and wrap a function nobody calls).
MAIN = "\0main"

# ONE target per panel, and `evidence` is the line it was READ from in this repo.
#
# This table used to carry nine entries and a list of "plausible alternate
# spellings" per entry. An audit resolved every one of them by AST + getattr:
#   * `item` -> views.items.build_embed / Restocker_main._build_item_panel_embed
#   * `me`   -> views.loyalty.build_embed / Restocker_main._build_loyalty_embed
#     Neither view module exists anywhere in the bot and neither main-module
#     function exists either: BOTH candidates dead, both panels could never mint
#     or resolve an address, while setup() reported success.
#   * `hive` / `manager` / `investor` named build_hive_embed / build_manager_embed
#     / build_investor_embed — functions no import site in the whole tree ever
#     mentions, so nothing here could confirm they exist.
# A guess that never resolves is indistinguishable from a panel that silently
# never stamps, which is exactly the failure this table exists to prevent. So the
# guesses are gone, and an entry that fails to resolve now RAISES (see
# `_wire_panels`) instead of being logged as "unbound in this build".
STAMP_TARGETS = [
    ("market", "views.market_settings", "build_embed",
     lambda a, k: k.get("mid") or k.get("market_id") or (a[0] if a else None),
     "imported by name at cogs/market.py:275, called at :289-291"),

    # `_listing_embed` is handed the listing ROW, not an id — the address has to
    # come out of the dict or every lot would share one code.
    ("lot", "cogs.land_exchange", "_listing_embed",
     lambda a, k: _from_row(k.get("listing") or (a[0] if a else None), "id"),
     "defined at cogs/land_exchange.py:177, signature (listing, bids=None)"),

    ("stock", MAIN, "_build_stock_panel_embed",
     lambda a, k: k.get("market_id") or (a[0] if a else None),
     "defined at Restocker_main.py:12931, signature (market_id)"),

    ("team", MAIN, "_team_perf_embed",
     lambda a, k: k.get("manager_id") or (a[0] if a else None),
     "defined at Restocker_main.py:3240, called at cogs/loops.py:765"),
]


class PanelWiringError(RuntimeError):
    """A STAMP_TARGETS entry did not resolve against the running bot.

    Deliberately fatal at cog load. The alternative — the behaviour this replaced
    — is a log line saying "unbound in this build" that nobody reads, a panel that
    never prints its address, and a `/go` code that can never be minted for it.
    """


def _from_row(row, key):
    """Entity id out of whatever the builder was handed: a row, or the id itself."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]          # sqlite3.Row
    except Exception:
        return row

_WIRED: set[str] = set()

# What to call each entity when talking to a user. Never the internal kind slug.
_KIND_WORD = {"market": "market", "hive": "hive", "lot": "auction lot",
              "item": "item", "team": "team", "user": "member"}


def _resolve_module(mod_path: str):
    """The live module object. MAIN means 'whatever Restocker_main is right now'.

    Importing Restocker_main by name when it is running as __main__ gives you a
    second, parallel copy of an 18k-line module: you would wrap a function nobody
    ever calls and the panel would silently never print its code.
    """
    if mod_path == MAIN:
        return core
    import importlib
    return importlib.import_module(mod_path)


def _rebind_aliases(fn_name: str, original, new) -> list[str]:
    """Re-point every module that captured `original` under `fn_name` at `new`.

    THE BUG THIS EXISTS FOR. `cogs/loops.py:64` does, at import time:

        _team_perf_embed = core._team_perf_embed

    That is a *value* copy into `cogs.loops`'s module globals. `setattr(core,
    "_team_perf_embed", wrapper)` rebinds the name on `Restocker_main` and does
    nothing whatsoever to `cogs.loops` — and `cogs/loops.py:765` is the ONLY
    caller of that function in the tree. Result before this fix: the team panel
    never printed a code, `panel_skus` never minted a `team` token, `/go` could
    never open a team, and `setup()` logged "stamped … team" and reported success.

    The module docstring at the top of this file guarded the *module identity*
    variant of this bug (import Restocker_main vs __main__) and missed the *alias
    capture* variant, which is the one that actually fired.

    Returns the module names it rebound, for the setup() log line.
    """
    hit = []
    for mod_name, m in list(sys.modules.items()):
        if m is None:
            continue
        try:
            if getattr(m, fn_name, None) is original:
                setattr(m, fn_name, new)
                hit.append(mod_name)
        except Exception:       # a module whose __getattr__ raises is not our problem
            continue
    return hit


def _wire_panels() -> tuple[list[str], list[str]]:
    """Make existing panels print their address, without touching their source.

    Wraps the panel's own embed builder so the footer is stamped after the panel
    has finished building it, then re-points every import-time alias of that
    builder at the wrapper (see `_rebind_aliases`). Idempotent and per-target
    guarded.

    Raises PanelWiringError if any target does not resolve. A panel that cannot
    stamp cannot be addressed by `/go`, and saying so at boot is the whole point:
    the previous behaviour returned it in `unbound` and carried on.

    Returns (bound_panel_keys, alias_rebind_notes).
    """
    bound, rebinds, missing = [], [], []
    for panel_key, mod_path, fn_name, extract, evidence in STAMP_TARGETS:
        try:
            mod = _resolve_module(mod_path)
            fn = getattr(mod, fn_name)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{panel_key}: {mod_path}.{fn_name} ({evidence}) — {e}")
            continue
        if not callable(fn):
            missing.append(f"{panel_key}: {mod_path}.{fn_name} ({evidence}) — not callable")
            continue
        tag = f"{mod_path}.{fn_name}"

        if tag in _WIRED:
            bound.append(panel_key)
            continue

        def _make(fn=fn, panel_key=panel_key, extract=extract):
            import functools
            import inspect

            def _stamp(embed, args, kwargs):
                try:
                    eid = extract(args, kwargs)
                except Exception:
                    eid = None
                try:
                    if isinstance(embed, discord.Embed):
                        panel_skus.stamp(embed, panel_key, eid)
                except Exception:
                    pass
                return embed

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def wrapper(*args, **kwargs):
                    return _stamp(await fn(*args, **kwargs), args, kwargs)
            else:
                @functools.wraps(fn)
                def wrapper(*args, **kwargs):
                    return _stamp(fn(*args, **kwargs), args, kwargs)
            wrapper.__panel_sku_wrapped__ = True
            return wrapper

        if getattr(fn, "__panel_sku_wrapped__", False):
            bound.append(panel_key)
            _WIRED.add(tag)
            continue
        wrapper = _make()
        setattr(mod, fn_name, wrapper)
        for alias_mod in _rebind_aliases(fn_name, fn, wrapper):
            if alias_mod != getattr(mod, "__name__", None):
                rebinds.append(f"{alias_mod}.{fn_name}")
        _WIRED.add(tag)
        bound.append(panel_key)

    if missing:
        raise PanelWiringError(
            "panel SKU stamp targets did not resolve — these panels would have "
            "silently never printed an address:\n  " + "\n  ".join(missing))
    return bound, rebinds


# ── Delegation to the panel's existing command ──────────────────────────────
def _find_command(bot, qualified: str):
    node = bot.tree
    cmd = None
    for part in qualified.split():
        getter = getattr(node, "get_command", None)
        if getter is None:
            return None
        cmd = getter(part)
        if cmd is None:
            return None
        node = cmd
    return cmd


async def _open(interaction: discord.Interaction, panel_key: str, entity_id) -> tuple[bool, str]:
    """Open `panel_key` on `entity_id`. Returns (opened, why_not)."""
    panel = panel_skus.PANELS.get(panel_key)
    if panel is None:
        return False, "That address points at a panel this build does not have."

    if panel.opener is not None:
        try:
            ok = await panel.opener(interaction, entity_id)
            return (ok is not False), ""
        except Exception as e:  # noqa: BLE001
            if log:
                log.warning("[/go] registered opener for %s failed: %s", panel_key, e)

    spec = ADAPTERS.get(panel_key) or {}
    names = spec.get("command") or []
    if isinstance(names, str):
        names = [names]
    cmd = None
    for qualified in names:
        cmd = _find_command(interaction.client, qualified)
        if cmd is not None:
            break
    if cmd is None:
        return False, (f"**{panel.title}** exists here, but `/{names[0] if names else panel_key}` "
                       f"is not registered in this build, so I cannot open it for you.")

    # RUN THE TARGET'S OWN CHECKS. `cmd.callback(...)` below is the raw function:
    # calling it directly skips `Command._check_can_run`, i.e. every
    # `@app_commands.checks.*` decorator and the tree/cog-level checks. Without
    # this line, `/go <code>` is a permission bypass for any panel command that
    # guards itself with a decorator rather than in its body — the two commands
    # this build can reach happen to guard in-body (`my market` checks
    # `_may_manage` at cogs/market.py:283; `realestate info` needs none), but
    # "safe because of what the targets currently happen to do" is not a check.
    try:
        may = await cmd._check_can_run(interaction)
    except Exception as e:  # noqa: BLE001 - a raised CheckFailure IS a refusal
        if log:
            log.info("[/go] %s refused for %s: %s", cmd.qualified_name,
                     getattr(interaction.user, "id", "?"), e)
        may = False
    if not may:
        return False, (f"You can't open **{panel.title}** — `/{cmd.qualified_name}` "
                       f"is not available to you.")

    kwargs = {}
    param = spec.get("param")
    if param and entity_id is not None:
        kwargs[param] = _coerce(cmd, param, entity_id)
    try:
        binding = getattr(cmd, "binding", None)
        if binding is not None:
            await cmd.callback(binding, interaction, **kwargs)
        else:
            await cmd.callback(interaction, **kwargs)
        return True, ""
    except Exception as e:  # noqa: BLE001
        if log:
            log.warning("[/go] delegate %s failed: %s", cmd.qualified_name, e)
        return False, f"`/{cmd.qualified_name}` refused to open: {e}"


def _coerce(cmd, param: str, value):
    """Match the target command's own annotation so an int parameter gets an int."""
    try:
        p = cmd._params.get(param)          # discord.app_commands.transformers.CommandParameter
        if p is not None and getattr(p.type, "name", "") == "integer":
            return int(value)
    except Exception:
        pass
    return value


# ── The picker shown when four characters were not enough ───────────────────
class _CandidateSelect(discord.ui.Select):
    def __init__(self, rows: list[dict], guild):
        options = []
        for r in rows[:25]:
            name = panel_skus.describe(r["kind"], r["entity_id"], guild=guild) or "(removed)"
            panel = panel_skus.PANELS.get(r["panel_key"])
            options.append(discord.SelectOption(
                label=name[:100],
                description=f"{panel.title if panel else r['kind']} · {r['panel_code']}.{r['token']}"[:100],
                value=r["token"]))
        super().__init__(placeholder="Which one did you mean?", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        # Re-resolve from the token, never from anything cached on this view.
        rows = panel_skus.resolve(self.values[0])
        if not rows:
            return await interaction.response.edit_message(
                content="That address no longer exists.", embed=None, view=None)
        r = rows[0]
        opened, why = await _open(interaction, r["panel_key"], r["entity_id"])
        if not opened and not interaction.response.is_done():
            await interaction.response.edit_message(content=why or "Could not open it.",
                                                    embed=None, view=None)


class CandidateView(discord.ui.View):
    def __init__(self, rows: list[dict], guild):
        super().__init__(timeout=120)
        self.add_item(_CandidateSelect(rows, guild))


# ── The cog ─────────────────────────────────────────────────────────────────
class PanelSkuCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _code_autocomplete(self, interaction: discord.Interaction, current: str):
        """Real names first, code second — the code is for people reading a
        screenshot, the picker is for people who are already here."""
        try:
            rows = panel_skus.suggestions_for(interaction.user.id, guild=interaction.guild,
                                              query=current or "", limit=25)
        except Exception:
            rows = []
        if not rows:
            return []
        return [app_commands.Choice(name=f"{r['label']} ({r['token']})"[:100], value=r["token"])
                for r in rows]

    @app_commands.command(
        name="go",
        description="Open a panel from the 4-character code printed on it")
    @app_commands.describe(code="The code from a panel footer — or leave blank and pick")
    @app_commands.autocomplete(code=_code_autocomplete)
    async def go(self, interaction: discord.Interaction, code: str = None):
        raw = (code or "").strip()

        if not raw:
            rows = panel_skus.suggestions_for(interaction.user.id, guild=interaction.guild,
                                              limit=25)
            if not rows:
                return await interaction.response.send_message(
                    "Nothing here has an address yet. Open any panel once — it prints its "
                    "code in the footer — and it will show up here.", ephemeral=True)
            as_rows = [{"token": r["token"], "kind": r["kind"], "entity_id": r["entity_id"],
                        "panel_key": panel_skus._KIND_PANEL[r["kind"]].key
                        if r["kind"] in panel_skus._KIND_PANEL else "market",
                        "panel_code": r["code"].rsplit(".", 1)[0]} for r in rows]
            return await interaction.response.send_message(
                "Pick what you want to open:", view=CandidateView(as_rows, interaction.guild),
                ephemeral=True)

        matches = panel_skus.resolve(raw)

        if not matches:
            return await interaction.response.send_message(
                f"No panel answers to `{panel_skus.normalise(raw)}`. Codes are four "
                f"characters from the footer of a panel — they never contain `l`, `o`, "
                f"`0` or `1`. Run `/go` with the box empty to pick from a list instead.",
                ephemeral=True)

        if len(matches) > 1:
            return await interaction.response.send_message(
                f"`{panel_skus.normalise(raw)}` could be more than one thing "
                f"(a character was misread) — pick the right one:",
                view=CandidateView(matches, interaction.guild), ephemeral=True)

        r = matches[0]
        name = panel_skus.describe(r["kind"], r["entity_id"], guild=interaction.guild)
        if name is None:
            what = _KIND_WORD.get(r["kind"], r["kind"])
            return await interaction.response.send_message(
                f"`{r['token']}` is a real address, but the {what} it pointed at has "
                f"since been removed. Nothing to open.", ephemeral=True)

        opened, why = await _open(interaction, r["panel_key"], r["entity_id"])
        if not opened and not interaction.response.is_done():
            await interaction.response.send_message(
                why or f"Could not open **{name}**.", ephemeral=True)


async def setup(bot):
    panel_skus.ensure_schema()
    # Deliberately NOT wrapped in try/except: _wire_panels raises PanelWiringError
    # when a target is gone, and a cog that fails to load is loud. A stamp target
    # that quietly does not bind is a panel with no address and a `/go` code that
    # can never exist — the exact silent-nothing this whole change is against.
    bound, rebinds = _wire_panels()
    if log:
        log.info("🏷 Panel SKUs: stamped %d panel(s) [%s]%s",
                 len(bound), ", ".join(bound) or "-",
                 f"; re-pointed import-time aliases: {', '.join(rebinds)}" if rebinds else "")
    await bot.add_cog(PanelSkuCog(bot))
