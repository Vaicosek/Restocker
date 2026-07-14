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

class ReportsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="monthly_report", description="Monthly coins payout report from fulfilled orders")


    @app_commands.describe(month="YYYY-MM (leave empty for current month)")
    @app_commands.default_permissions(manage_guild=True)
    async def monthly_report(self, interaction: discord.Interaction, month: Optional[str] = None):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)


        now = datetime.now(timezone.utc)
        if month:
            try:
                y_s, m_s = month.strip().split("-", 1)
                year = int(y_s)
                mon = int(m_s)
                if mon < 1 or mon > 12:
                    raise ValueError
            except Exception:
                return await interaction.followup.send("❌ Use `YYYY-MM` (example `2026-01`).", ephemeral=True)
        else:
            year, mon = now.year, now.month

        start_dt, end_dt = _month_bounds_utc(year, mon)


        try:
            items_data = _load_items()
        except Exception:
            items_data = {"items": {}}

        data = load_orders()
        orders = list(data.get("orders", []) or [])

        total_coins = 0.0
        included_orders = 0
        skipped_no_ts = 0


        per_item = {}

        per_prod = {}

        for o in orders:
            if not isinstance(o, dict):
                continue

            if str(o.get("status", "")).lower() != "fulfilled":
                continue

            ts = _order_report_timestamp(o)
            if not ts:
                skipped_no_ts += 1
                continue

            if not (start_dt <= ts < end_dt):
                continue

            included_orders += 1

            item_name = str(o.get("item") or "Unknown item")
            per_item.setdefault(item_name, {"qty": 0, "coins": 0.0, "orders": 0})
            per_item[item_name]["orders"] += 1

            claims = _claims_iter(o)


            if claims:
                for c in claims:
                    try:
                        qty = int(c.get("qty", 0) or 0)
                    except Exception:
                        qty = 0
                    if qty <= 0:
                        continue

                    coins = float(_coins_for_pieces(o, qty, items_data))
                    total_coins += coins

                    per_item[item_name]["qty"] += qty
                    per_item[item_name]["coins"] += coins

                    pk = _producer_key(c)
                    per_prod.setdefault(pk, {"qty": 0, "coins": 0.0})
                    per_prod[pk]["qty"] += qty
                    per_prod[pk]["coins"] += coins
            else:
                try:
                    qty = int(o.get("requested", 0) or 0)
                except Exception:
                    qty = 0
                if qty > 0:
                    coins = float(_coins_for_pieces(o, qty, items_data))
                    total_coins += coins

                    per_item[item_name]["qty"] += qty
                    per_item[item_name]["coins"] += coins


        embed = discord.Embed(
            title=f"📊 Monthly Financial Report — {year:04d}-{mon:02d} (UTC)",
            color=discord.Color.gold()
        )

        embed.add_field(name="Included fulfilled orders", value=str(included_orders), inline=True)
        embed.add_field(name="Total payout", value=f"≈ **{int(round(total_coins))} coins**", inline=True)
        if skipped_no_ts:
            embed.add_field(name="Skipped (no timestamp)", value=str(skipped_no_ts), inline=True)


        top_items = sorted(per_item.items(), key=lambda kv: kv[1]["coins"], reverse=True)[:10]
        if top_items:
            lines = []
            for name, info in top_items:
                lines.append(
                    f"• **{name}** — qty **{info['qty']}** · ≈ **{int(round(info['coins']))}c** · {info['orders']} orders"
                )
            embed.add_field(name="Top items (by coins)", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Top items", value="—", inline=False)


        top_prod = sorted(per_prod.items(), key=lambda kv: kv[1]["coins"], reverse=True)[:10]
        if top_prod:
            lines = []
            for who, info in top_prod:
                lines.append(
                    f"• **{who}** — qty **{info['qty']}** · ≈ **{int(round(info['coins']))}c**"
                )
            embed.add_field(name="Top producers (by coins)", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Top producers", value="—", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="csn",
        description="CSN report — auto-detects export or monthly CSV, saves history, charts & optional restock"
    )


    @app_commands.describe(
        file="Upload csn_export_*.csv or csn_monthly_*.csv (omit to use latest local file)",
        charts="Post bar charts (requires matplotlib). Default: True",
        restock="Show restock preview (managers only). Default: False",
        confirm_restock="Set True to actually create restock orders (only with restock=True). Default: False",
        market_id="Which market to record this data for (default: main). Use /market list to see IDs.",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    async def csn(self, 
        interaction: discord.Interaction,
        file: discord.Attachment | None = None,
        charts: bool = True,
        restock: bool = False,
        confirm_restock: bool = False,
        market_id: str = DEFAULT_MARKET_ID,
    ):
        await interaction.response.defer(thinking=True)

        csv_text = ""
        source_label = ""

        if file:
            if not (file.filename or "").lower().endswith(".csv"):
                return await interaction.followup.send("❌ Please upload a `.csv` file.", ephemeral=True)
            try:
                csv_text = (await file.read()).decode("utf-8", errors="replace")
                source_label = file.filename or "uploaded.csv"
            except Exception as e:
                return await interaction.followup.send(f"❌ Could not read file: `{e}`", ephemeral=True)
        else:
            path = (_find_latest_csv("csn_monthly_*.csv") or _find_latest_csv("csn_export_*.csv"))
            if not path:
                return await interaction.followup.send(
                    "❌ No CSN CSV found next to the bot. Upload one or copy it from your `.minecraft` folder."
                )
            with open(path, encoding="utf-8") as f:
                csv_text = f.read()
            source_label = os.path.basename(path)

        csv_type = _detect_csv_type(csv_text, source_label)
        period_from = period_to = None

        if csv_type == "stock":
            # Live shop-stock snapshot (csn_stock_*.csv) — record barrel fullness.
            # Market routing: an explicit market_id param wins; otherwise honor the
            # CSV's own "# MARKET,<id>,<code>" header if that market exists.
            rows = _parse_stock_csv(csv_text)
            if not rows:
                return await interaction.followup.send(f"❌ No stock rows found in `{source_label}`.")
            mid = market_id
            try:
                csv_mid, csv_code = _extract_market_info(csv_text)
            except Exception:
                csv_mid, csv_code = None, None
            if csv_mid and mid == DEFAULT_MARKET_ID and _get_market(csv_mid):
                mid = csv_mid
            elif mid == DEFAULT_MARKET_ID and csv_code:
                # market_id didn't match a registered market (e.g. a typo like
                # 'viridianmarke') — fall back to the verification code, which uniquely
                # identifies the market, instead of dumping into the default market.
                _by_code = _market_id_by_code(csv_code)
                if _by_code:
                    mid = _by_code
            await _record_stock_report(rows, mid, interaction.channel, source_label)
            return await interaction.followup.send(
                f"✅ Live stock snapshot recorded for `{mid}` — **{len(rows)}** item(s).\n"
                f"See `/inventory stock market_id:{mid}` or the website's STOCK column.")

        if csv_type == "monthly":
            items, income, spent = _parse_monthly_csv(csv_text)
            m = re.search(r"(\d{4})-(\d{2})", source_label)
            month_key = f"{m.group(1)}-{m.group(2)}" if m else utcnow_dt().strftime("%Y-%m")
            try:
                from datetime import date as _date
                month_label = _date(int(month_key[:4]), int(month_key[5:7]), 1).strftime("%B %Y")
            except Exception:
                month_label = month_key
            title = f"📅 Monthly Sales Report — {month_label}"
            title_suffix = f" — {month_label}"

        elif csv_type == "export":
            items, income, spent, period_from, period_to = _parse_export_csv(csv_text)
            period_str = f" — {period_from} → {period_to}" if period_from and period_to else ""
            title = f"📊 CSN Sales Report{period_str}"
            month_key = utcnow_dt().strftime("%Y-%m")
            try:
                from datetime import date as _date
                month_label = _date(int(month_key[:4]), int(month_key[5:7]), 1).strftime("%B %Y")
            except Exception:
                month_label = month_key
            title_suffix = ""

        else:
            return await interaction.followup.send(
                "❌ Could not detect CSV type.\n"
                "Make sure the file is named `csn_export_*.csv`, `csn_monthly_*.csv`, or `csn_stock_*.csv`."
            )

        if not items:
            return await interaction.followup.send(f"❌ No sales data found in `{source_label}`.")

        _record_to_market_history(market_id, month_key, month_label, source_label, income, spent, items)
        if market_id == DEFAULT_MARKET_ID:
            _record_to_history(month_key, month_label, source_label, income, spent, items)

        # Resolve which market these items belong to: explicit param wins, else the
        # CSV's own "# MARKET,<id>,<code>" header (same routing the stock import uses),
        # else the default. The monthly/export path used to always tag items to the
        # command's market_id and IGNORE existing rows, so per-market dashboards showed
        # nothing for a market whose items were first imported elsewhere.
        eff_mid = market_id
        try:
            _csv_mid, _csv_code = _extract_market_info(csv_text)
        except Exception:
            _csv_mid, _csv_code = None, None
        if _csv_mid and market_id == DEFAULT_MARKET_ID and _get_market(_csv_mid):
            eff_mid = _csv_mid
        elif market_id == DEFAULT_MARKET_ID and _csv_code:
            # id didn't match a registered market (e.g. typo) — fall back to the
            # verification code, which uniquely identifies the market.
            _by_code = _market_id_by_code(_csv_code)
            if _by_code:
                eff_mid = _by_code
        # Re-attribute items only when an explicit, real (non-default) market is
        # targeted. The catalog is keyed by name, so INSERT OR IGNORE left items stuck
        # under whichever market imported them first; re-importing under the right
        # market now moves them. Default imports stay non-clobbering so a stray default
        # import never yanks items out of their market.
        _reattribute = eff_mid != DEFAULT_MARKET_ID
        _set_market_sql = ", market_id=excluded.market_id" if _reattribute else ""
        try:
            import Restocker_db as _db_csn
            with _db_csn.db() as _conn:
                for item_name, v in items.items():
                    sold_qty  = v.get("sold_qty", 0)
                    net_coins = v.get("net_coins", 0.0)
                    estimated_coin = 0
                    if sold_qty > 0 and net_coins > 0:
                        if csv_type == "export":
                            estimated_coin = max(1, round(net_coins / sold_qty))
                        else:
                            bought_qty = v.get("bought_qty", 0)
                            if bought_qty == 0:
                                estimated_coin = max(1, round(net_coins / sold_qty))
                    _stack_sz = _detect_stack_size(item_name)
                    _stackable = 1 if _stack_sz > 1 else 0
                    _conn.execute(
                        "INSERT INTO items "
                        "(name, coin, stock, unit_type, stackable, stack_size, barrel_slots, market_id) "
                        "VALUES (?, ?, 0, 'pieces', ?, ?, 54, ?) "
                        "ON CONFLICT(name) DO UPDATE SET "
                        "  coin = CASE WHEN items.coin=0 THEN excluded.coin ELSE items.coin END"
                        + _set_market_sql,
                        (item_name, estimated_coin, _stackable, _stack_sz, eff_mid)
                    )
        except Exception as _e:
            log.warning("[csn] auto-register items failed: %s", _e)

        extra_fields = []
        if restock:
            if not is_manager(interaction):
                extra_fields.append(("⛔ Restock", "You need the @Manager role to create orders.", False))
            else:
                to_order, skipped_active, skipped_unknown = _build_restock_plan(items)
                if not to_order:
                    extra_fields.append(("🔁 Restock", "No new items to restock (all have active orders or are unknown).", False))
                elif not confirm_restock:
                    preview = "\n".join(f"🔸 **{item}** — `{qty:,}` pcs" for item, qty, _ in to_order[:10])
                    if len(to_order) > 10:
                        preview += f"\n*…and {len(to_order) - 10} more*"
                    preview += (
                        f"\n\n`{skipped_active}` skipped (active order) · `{skipped_unknown}` unknown\n"
                        "Run again with **confirm\\_restock: True** to create the orders."
                    )
                    extra_fields.append(("🔍 Restock Preview (dry run)", preview, False))
                else:
                    created = _create_restock_orders(to_order)
                    extra_fields.append((
                        "✅ Restock Orders Created",
                        f"`{created}` orders created · `{skipped_active}` skipped · `{skipped_unknown}` unknown",
                        False,
                    ))

        embed, overflow = _build_csn_embed(title, items, income, spent, source_label, extra_fields)

        files = []
        if charts:
            if not _MATPLOTLIB_OK:
                embed.set_footer(text=(embed.footer.text or "") + "  •  📊 charts on dashboard.vaicosmarket.com")
            else:
                try:
                    _hist = _load_csn_for_market(market_id).get("months", {}) or {}
                    _hist_months = [_hist[k] for k in sorted(_hist.keys())]
                except Exception:
                    _hist_months = None
                chart_data = _generate_charts(items, title_suffix, _hist_months)
                files = [discord.File(io.BytesIO(c), filename=f"csn_chart_{i+1}.png") for i, c in enumerate(chart_data)]
                if files:
                    embed.set_image(url="attachment://csn_chart_1.png")

        # Link to the full web report (same data, sortable, nothing to download) —
        # people can open and go through the entire month there.
        try:
            embed.add_field(
                name="📊 Full report",
                value=(f"[Open the complete sortable report]"
                       f"(https://dashboard.vaicosmarket.com/report/{market_id}/{month_key})"
                       f"  ·  or open the attached `.html`"),
                inline=False)
        except Exception:
            pass

        # Attach the COMPLETE report as a self-contained, sortable HTML file so people
        # can open and go through the whole month (the embed only shows the top rows).
        try:
            _mkt_name = (_get_market(market_id) or {}).get("name", market_id)
            _report_html = _render_full_report_html(title, _mkt_name, month_label, items, income, spent)
            files.append(discord.File(
                io.BytesIO(_report_html.encode("utf-8")),
                filename=f"report_{market_id}_{month_key}.html"))
        except Exception as _e:
            log.warning("[csn] full-report html failed: %s", _e)

        await interaction.followup.send(embed=embed, files=files)

        if overflow:
            extra = "\n".join(overflow)
            if len(extra) > 1900:
                extra = extra[:1900] + "\n…(truncated)"
            await interaction.followup.send(f"📋 **…continued**\n{extra}")

    @app_commands.command(
        name="csn_history",
        description="View saved monthly sales history and all-time totals"
    )


    @app_commands.describe(
        item="Look up a specific item across all months",
        charts="Show all-time top items chart (requires matplotlib). Default: True",
    )
    @app_commands.autocomplete(item=any_item_autocomplete)
    async def csn_history(self,
        interaction: discord.Interaction,
        item: str | None = None,
        charts: bool = True,
    ):
        await interaction.response.defer(thinking=True)

        history = _load_csn_history()
        months: dict = history.get("months") or {}

        if not months:
            return await interaction.followup.send(
                "📭 No history saved yet. Run `/csn` with a CSV file first."
            )

        sorted_months = sorted(months.items())

        all_time_income = sum(m["income"] for m in months.values())
        all_time_spent  = sum(m["spent"]  for m in months.values())
        all_time_net    = sum(m["net"]    for m in months.values())
        aliases = _load_brew_aliases()
        all_items: dict = {}
        for md in months.values():
            for iname, v in (md.get("items") or {}).items():
                display = aliases.get(iname, iname)
                d = all_items.setdefault(display, {"sold_qty": 0, "net_coins": 0.0})
                d["sold_qty"]  += v.get("sold_qty", 0)
                d["net_coins"] += v.get("net_coins", 0.0)

        if item:
            item_lower = item.lower()
            matches = {k: v for k, v in all_items.items() if item_lower in k.lower()}
            if not matches:
                return await interaction.followup.send(f"❌ No history found for `{item}`.")

            embed = discord.Embed(title=f"🔍 Item History — {item}", color=0x5865F2)
            for iname, totals in sorted(matches.items(), key=lambda x: -x[1]["net_coins"])[:5]:
                month_lines = []
                for mk, md in sorted_months:
                    v = (md.get("items") or {}).get(iname)
                    if v and v.get("sold_qty", 0) > 0:
                        month_lines.append(
                            f"**{md.get('label', mk)}** — `{v['sold_qty']:,}` sold · `{int(v['net_coins']):,}` 🪙"
                        )
                if month_lines:
                    embed.add_field(
                        name=f"**{iname}** — all time: `{totals['sold_qty']:,}` sold · `{int(totals['net_coins']):,}` 🪙",
                        value="\n".join(month_lines),
                        inline=False,
                    )
            await interaction.followup.send(embed=embed)
            return

        embed = discord.Embed(title="📈 CSN Sales History — All Time", color=0x5865F2)

        embed.add_field(
            name="🌐 All-Time Summary",
            value=(
                f"**Income:** `{int(all_time_income):,}` 🪙\n"
                f"**Spent:**  `{int(all_time_spent):,}` 🪙\n"
                f"**Net:**    `{int(all_time_net):+,}` 🪙\n"
                f"**Months recorded:** `{len(months)}`"
            ),
            inline=True,
        )

        top5 = sorted(all_items.items(), key=lambda x: x[1]["net_coins"], reverse=True)[:5]
        if top5:
            embed.add_field(
                name="🏆 All-Time Top Earners",
                value="\n".join(
                    f"`{i}.` **{n}** — `{int(v['net_coins']):,}` 🪙 · `{v['sold_qty']:,}` sold"
                    for i, (n, v) in enumerate(top5, 1)
                ),
                inline=True,
            )

        embed.add_field(name="\u200b", value="\u200b", inline=True)

        month_lines = []
        for mk, md in reversed(sorted_months):
            net    = md.get("net", 0)
            income = md.get("income", 0)
            arrow  = "📈" if net >= 0 else "📉"
            month_lines.append(
                f"{arrow} **{md.get('label', mk)}** — `{int(income):,}` income · `{int(net):+,}` net"
            )
        if month_lines:
            chunk = "\n".join(month_lines[:15])
            if len(month_lines) > 15:
                chunk += f"\n*…and {len(month_lines) - 15} older months*"
            embed.add_field(name="📅 Month-by-Month", value=chunk, inline=False)

        files = []
        if charts and all_items:
            if not _MATPLOTLIB_OK:
                embed.set_footer(text="📊 Interactive charts on the dashboard → dashboard.vaicosmarket.com")
            else:
                chart_data = _generate_charts(all_items, " — All Time")
                files = [discord.File(io.BytesIO(c), filename=f"history_chart_{i+1}.png") for i, c in enumerate(chart_data)]
                if files:
                    embed.set_image(url="attachment://history_chart_1.png")

        embed.set_footer(text="Tip: /csn_history item:<name> to look up a specific item across months")
        await interaction.followup.send(embed=embed, files=files)

    @app_commands.command(
        name="import_earnings",
        description="(Manager) Import a CSV/Excel earnings summary (one row per month) into a market",
    )
    @app_commands.describe(
        file="A .csv or .xlsx with a row per month (columns like Month/Period, Revenue/Income, Profit/Net)",
        market_id="Which market to import into (default: main). Use /market list to see IDs.",
        replace="Wipe this market's existing months (incl. per-item sales) first, then import only the file. Default: False (upsert).",
    )
    @app_commands.autocomplete(market_id=_market_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def import_earnings(self, 
        interaction: discord.Interaction,
        file: discord.Attachment,
        market_id: str = DEFAULT_MARKET_ID,
        replace: bool = False,
    ):
        if not is_manager(interaction):
            return await interaction.response.send_message("⛔ Managers only.", ephemeral=True)
        await interaction.response.defer(thinking=True)

        fname = (file.filename or "").lower()
        if not (fname.endswith(".csv") or fname.endswith(".xlsx")):
            return await interaction.followup.send("❌ Please attach a `.csv` or `.xlsx` file.")

        try:
            rows = _read_tabular(await file.read(), fname)
        except Exception as e:
            return await interaction.followup.send(f"❌ Couldn't read the file: {e}")
        if not rows:
            return await interaction.followup.send("❌ The file appears to be empty.")

        parsed, skipped, header_str = _parse_earnings_rows(rows)
        if not parsed:
            return await interaction.followup.send(
                "❌ Couldn't find the right columns. I need a **month/period** column plus an "
                "**income/revenue** column and/or a **net/profit** column.\n"
                f"Header I detected: `{header_str or 'none'}`")

        if market_id not in (_load_markets().get("markets") or {}):
            return await interaction.followup.send(
                f"❌ Market `{market_id}` not found. Run `/market list` to see valid IDs.")

        if replace:
            try:
                _save_csn_for_market(market_id, {"months": {}})
                if market_id == DEFAULT_MARKET_ID:
                    _save_csn_history({"months": {}})
            except Exception as e:
                log.warning("[import_earnings] replace-clear failed for %s: %s", market_id, e)

        for m in parsed:
            _record_to_market_history(market_id, m["key"], m["label"],
                                      f"import:{file.filename}", m["income"], m["spent"], {})
            if market_id == DEFAULT_MARKET_ID:
                _record_to_history(m["key"], m["label"],
                                   f"import:{file.filename}", m["income"], m["spent"], {})

        total_inc = sum(m["income"] for m in parsed)
        total_net = sum(m["income"] - m["spent"] for m in parsed)
        mkt = _get_market(market_id) or {}
        msg = [
            f"✅ Imported **{len(parsed)}** month(s) into **{mkt.get('name', market_id)}** (`{market_id}`)"
            + (" — **replaced** all prior history." if replace else "."),
            f"Range `{parsed[0]['label']}` → `{parsed[-1]['label']}`  ·  "
            f"total income `{int(total_inc):,}` · total net `{int(total_net):+,}`",
        ]
        if skipped:
            msg.append(f"⏭️ Skipped {skipped} non-month row(s) (totals/averages/blank).")
        if replace:
            msg.append("Old months and their per-item sales data were cleared (Prices tab will no "
                       "longer show CSN items for this market). The dashboard updates on next load.")
        else:
            msg.append("Existing months with the same key were overwritten; others kept. "
                       "The website dashboard updates on next load.")
        await interaction.followup.send("\n".join(msg))


    @app_commands.command(name="csn_audit",
                          description="(Manager) Verify a market's CSN month: dedup stats, net, and pricing")
    @app_commands.describe(market_id="Market to audit", month="YYYY-MM (default: latest)")
    @app_commands.autocomplete(market_id=_market_autocomplete)
    @app_commands.default_permissions(manage_guild=True)
    async def csn_audit(self, interaction: discord.Interaction, market_id: str, month: str = ""):
        if not is_manager(interaction):
            return await interaction.response.send_message("Managers only.", ephemeral=True)
        import json as _json
        import Restocker_db as _db
        hist = (_load_csn_for_market(market_id) or {}).get("months", {}) or {}
        if not hist:
            return await interaction.response.send_message(f"No CSN history for `{market_id}`.", ephemeral=True)
        mk = (month or "").strip() or max(hist.keys())
        if mk not in hist:
            have = ", ".join(sorted(hist)[-6:])
            return await interaction.response.send_message(
                f"No `{mk}` for `{market_id}`. Have: {have}", ephemeral=True)
        md = hist[mk]
        income = float(md.get("income", 0) or 0)
        spent = float(md.get("spent", 0) or 0)
        net = float(md.get("net", income - spent) or 0)
        items = md.get("items", {}) or {}
        sold_units = sum(int(v.get("sold_qty", 0) or 0) for v in items.values())

        meta = {}
        try:
            raw = _db.get_config(f"csn_meta:{market_id}:{mk}")
            meta = _json.loads(raw) if raw else {}
        except Exception:
            meta = {}

        prior = [float(v.get("net", 0) or 0) for k, v in hist.items()
                 if k != mk and float(v.get("net", 0) or 0) > 0]
        avg = sum(prior) / len(prior) if prior else 0.0
        anom = avg > 0 and net > 3.0 * avg

        listing = _db.get_market_shares(market_id) or {}
        price = float(listing.get("share_price") or 0)
        shares_out = float(listing.get("shares_outstanding") or 0)
        try:
            f = _fundamental_for_market(market_id)
            fundamental = f[0] if f else None
        except Exception:
            fundamental = None

        m = _get_market(market_id) or {}
        title = f"CSN audit — {m.get('name', market_id)} · {mk}"
        embed = discord.Embed(title=title, color=(0xE5484D if anom else 0x22FF7A))
        embed.add_field(name="Net",
                        value=f"income `{income:,.0f}` − spent `{spent:,.0f}` = **{net:,.0f}**", inline=False)
        embed.add_field(name="Volume", value=f"{len(items)} unique items · `{sold_units:,}` sold", inline=True)
        if meta:
            embed.add_field(
                name="Parse integrity",
                value=(f"{meta.get('blocks', '?')} RUN block(s), **{meta.get('dupes_removed', 0)}** duplicate(s) "
                       f"removed · mode **{meta.get('mode', '?')}**"),
                inline=False)
        else:
            embed.add_field(name="Parse integrity",
                            value="no parse metadata (imported before audit existed, or not a monthly file)",
                            inline=False)
        embed.add_field(name="Trailing avg net", value=f"`{avg:,.0f}` ({len(prior)} mo)", inline=True)
        if price > 0:
            embed.add_field(
                name="Pricing",
                value=(f"share `{price:,.2f}` · fundamental `{(fundamental or 0):,.2f}` · "
                       f"{shares_out:,.0f} shares · clamp ±{STOCK_MAX_REANCHOR_MOVE * 100:.0f}%/report"),
                inline=False)
        if anom:
            embed.add_field(name="⚠ Anomaly",
                            value=(f"net is **{net / avg:.1f}×** the trailing average — verify for duplicate "
                                   f"runs / un-cleared CSN before trusting it."),
                            inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(ReportsCog(bot))
