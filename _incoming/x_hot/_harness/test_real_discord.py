"""Loads the patched cog against the REAL discord.py (2.7.x) — nothing about Discord is
stubbed here. This is what catches an @app_commands.describe() naming a parameter that
does not exist, a bad Group/command signature, or a DynamicItem template that won't
compile: discord.py validates all of those at class-body evaluation time."""
import sys, os, types, importlib

TARGET = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/build/hotfix"
sys.path.insert(0, TARGET)

# Only the NON-discord dependencies are stubbed.
m = types.ModuleType("Restocker_main")
m.add_coins = m.deduct_coins = lambda *a, **kw: (0, 0)
m._credit_platform_balance = lambda *a, **kw: 0
m.log = types.SimpleNamespace(warning=print, error=print, info=print)
m.bot = types.SimpleNamespace(add_dynamic_items=lambda c: None, is_ready=lambda: False)
m.is_manager = lambda i: False
async def _ac(interaction, current):
    return []
m._market_autocomplete = m.any_item_autocomplete = _ac
m._get_market = lambda x: None
m.utcnow_iso = lambda: ""
sys.modules["Restocker_main"] = m
sys.modules["Restocker_db"] = types.ModuleType("Restocker_db")
cogs = types.ModuleType("cogs"); cogs.__path__ = [os.path.join(TARGET, "cogs")]
sys.modules["cogs"] = cogs
v = types.ModuleType("cogs.valuation"); v.value_plot = lambda *a, **kw: {}
sys.modules["cogs.valuation"] = v

import discord
LX = importlib.import_module("cogs.land_exchange")
print(f"  [PASS] module imports under real discord.py {discord.__version__}")

cog = LX.LandExchangeCog(m.bot)
print("  [PASS] LandExchangeCog instantiates")

names = sorted(c.qualified_name for c in LX.LandExchangeCog.realestate.walk_commands())
print(f"  [PASS] /realestate subcommands built by real app_commands: {names}")

cfg = LX.LandExchangeCog.realestate.get_command("config")
params = [p.name for p in cfg.parameters]
descs = {p.name: bool(p.description and p.description != "…") for p in cfg.parameters}
assert "max_auction_days" in params, params
assert descs["max_auction_days"], "describe() did not attach to max_auction_days"
print(f"  [PASS] /realestate config exposes max_auction_days with a real description")
print(f"         params: {params}")

for cls, tmpl in ((LX.BidButton, "rex:bid:7"), (LX.BuyButton, "rex:buy:7"),
                  (LX.NotifyButton, "rex:notify:land")):
    assert cls.__discord_ui_compiled_template__.match(tmpl), cls
print("  [PASS] restart-safe DynamicItem templates still compile and match")

assert LX.LandExchangeCog.auction_sweep_loop.minutes == 1
print("  [PASS] auction_sweep_loop is still a real 1-minute tasks.loop")
print(f"  [PASS] DEF knobs: {sorted(LX.DEF)}")
