"""CSN / earnings reports commands (extracted from Restocker_main)."""
import sys
import discord
from discord import app_commands
from discord.ext import commands

from datetime import datetime
from typing import Optional
import os
import re

core = sys.modules.get("Restocker_main") or sys.modules["__main__"]
DEFAULT_MARKET_ID = core.DEFAULT_MARKET_ID
_MATPLOTLIB_OK = core._MATPLOTLIB_OK
_build_csn_embed = core._build_csn_embed
_render_full_report_html = core._render_full_report_html
_build_restock_plan = core._build_restock_plan
_claims_iter = core._claims_iter
_coins_for_pieces = core._coins_for_pieces
_create_restock_orders = core._create_restock_orders
_detect_csv_type = core._detect_csv_type
_detect_stack_size = core._detect_stack_size
_parse_stock_csv = core._parse_stock_csv
_record_stock_report = core._record_stock_report
_extract_market_info = core._extract_market_info
_market_id_by_code = core._market_id_by_code
_find_latest_csv = core._find_latest_csv
_generate_charts = core._generate_charts
_get_market = core._get_market
_fundamental_for_market = core._fundamental_for_market
STOCK_MAX_REANCHOR_MOVE = core.STOCK_MAX_REANCHOR_MOVE
_load_brew_aliases = core._load_brew_aliases
_load_csn_for_market = core._load_csn_for_market
_load_csn_history = core._load_csn_history
_load_items = core._load_items
_load_markets = core._load_markets
_market_autocomplete = core._market_autocomplete
any_item_autocomplete = core.any_item_autocomplete
_month_bounds_utc = core._month_bounds_utc
_order_report_timestamp = core._order_report_timestamp
_parse_earnings_rows = core._parse_earnings_rows
_parse_export_csv = core._parse_export_csv
_parse_monthly_csv = core._parse_monthly_csv
_producer_key = core._producer_key
_read_tabular = core._read_tabular
_record_to_history = core._record_to_history
_record_to_market_history = core._record_to_market_history
_save_csn_for_market = core._save_csn_for_market
_save_csn_history = core._save_csn_history
io = core.io
is_manager = core.is_manager
load_orders = core.load_orders
log = core.log
timezone = core.timezone
utcnow_dt = core.utcnow_dt


async def _recent_months_autocomplete(interaction: discord.Interaction, current: str):
    """The last 18 calendar months as YYYY-MM — month pickers, so nobody has to hand-
    type the format or guess which month."""
    now = utcnow_dt()
    out = []
    y, mo = now.year, now.month
    for _ in range(18):
        key = f"{y:04d}-{mo:02d}"
        if not current or current.lower() in key:
            out.append(app_commands.Choice(name=key, value=key))
        mo -= 1
        if mo == 0:
            mo = 12; y -= 1
    return out[:25]


async def _csn_month_autocomplete(interaction: discord.Interaction, current: str):
    """The CSN months actually on file for the market_id being typed — for /csn_audit."""
    market_id = getattr(interaction.namespace, "market_id", None) or ""
    try:
        months = (_load_csn_for_market(market_id).get("months") or {})
    except Exception:
        months = {}
    out = []
    for mk, md in sorted(months.items(), reverse=True):
        label = (md.get("label", mk) if isinstance(md, dict) else mk)
        if not current or current.lower() in mk.lower() or current.lower() in str(label).lower():
            out.append(app_commands.Choice(name=str(label), value=mk))
    return out[:25]


class ReportsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot





async def setup(bot):
    await bot.add_cog(ReportsCog(bot))
