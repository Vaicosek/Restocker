#!/usr/bin/env python3
"""
Company valuation engine — GEX methodology.

Values a company three ways so you never over/under-count:
  1. DCF   — intrinsic value of ALL free cash flow (owner's ceiling).
  2. DDM   — value of the DIVIDEND stream only (minority investor's floor).
  3. Market — the traded price, for comparison.

Method is the Crimson Investment Bank DCF, VALIDATED to reproduce their original
GEX IPO figure of $33.30M equity / $28.99M EV / $25.92M terminal value.

Usage (as a script, edit the INPUTS block) or import value():
    from value_company import value
    r = value(fwd_rev=4_000_000, cash=4_311_630, shares=40_000,
              dividend_mo=171_000, traded_price=1899)
"""
from dataclasses import dataclass

# ── Default assumptions (from the bank's IPO memo — override per company) ──────
WACC_MONTHLY   = 0.0441   # monthly discount rate
NWC_PCT        = 0.30     # working capital as % of sales
CAPEX_PCT      = 0.07     # capex as % of sales
GROWTH_MONTHLY = 0.05     # revenue growth per month (months 2-5)
TERMINAL_G     = -0.01    # perpetuity growth (negative = conservative decline; fits a server economy)


def _dcf(fwd_rev, cash, debt, wacc, nwc, capex, growth, term_g,
         t0_rev=None, t0_capex_abs=None):
    """Enterprise + equity value from the 5-month forward DCF.
    t0 = last REALIZED month (revenue - capex, no NWC), undiscounted.
    Months 1-5 = forward (FCF margin = 1 - nwc - capex), growth starts month 2.
    Terminal value at month 5 via Gordon growth."""
    fcf_margin = 1 - nwc - capex
    t0_rev = t0_rev if t0_rev is not None else fwd_rev
    fcf0 = t0_rev - (t0_capex_abs if t0_capex_abs is not None else t0_rev * capex)
    pv = fcf0
    last = None
    schedule = [("m0 (realized)", t0_rev, fcf0, fcf0)]
    for t in range(1, 6):
        rev = fwd_rev * ((1 + growth) ** (t - 1))
        fcf = rev * fcf_margin
        p = fcf / ((1 + wacc) ** t)
        pv += p
        last = fcf
        schedule.append((f"m{t}", rev, fcf, p))
    tv = last * (1 + term_g) / (wacc - term_g)
    pv_tv = tv / ((1 + wacc) ** 5)
    ev = pv + pv_tv
    equity = ev + cash - debt
    return dict(ev=ev, equity=equity, tv=tv, pv_tv=pv_tv, schedule=schedule)


def _ddm(dividend_mo, wacc, term_g):
    """Value of the dividend stream as a growing perpetuity (Gordon)."""
    if dividend_mo is None or dividend_mo <= 0:
        return 0.0
    return dividend_mo * (1 + term_g) / (wacc - term_g)


@dataclass
class Result:
    dcf_equity: float
    dcf_per_share: float
    ddm_value: float
    ddm_per_share: float
    payout_pct: float | None
    market_cap: float | None
    market_per_share: float | None
    fair_minority_low: float
    fair_minority_high: float
    detail: dict


def value(fwd_rev, cash, shares, *, debt=0.0, dividend_mo=None, monthly_profit=None,
          traded_price=None, wacc=WACC_MONTHLY, nwc=NWC_PCT, capex=CAPEX_PCT,
          growth=GROWTH_MONTHLY, term_g=TERMINAL_G, t0_rev=None, t0_capex_abs=None) -> Result:
    d = _dcf(fwd_rev, cash, debt, wacc, nwc, capex, growth, term_g, t0_rev, t0_capex_abs)
    dcf_ps = d["equity"] / shares
    ddm = _ddm(dividend_mo, wacc, term_g)
    ddm_ps = ddm / shares
    payout = (dividend_mo / monthly_profit * 100) if (dividend_mo and monthly_profit) else None
    mcap = (traded_price * shares) if traded_price else None
    # Minority shares are worth between the dividend floor and the DCF ceiling.
    return Result(
        dcf_equity=d["equity"], dcf_per_share=dcf_ps,
        ddm_value=ddm, ddm_per_share=ddm_ps, payout_pct=payout,
        market_cap=mcap, market_per_share=traded_price,
        fair_minority_low=ddm_ps, fair_minority_high=dcf_ps, detail=d)


def _fmt(n):
    return f"{n:,.0f}"


def report(res: Result, shares: int, name="Company"):
    print(f"=== {name} valuation ===")
    print("DCF schedule (forward FCF, PV):")
    for lbl, rev, fcf, pv in res.detail["schedule"]:
        print(f"  {lbl:<14} rev {_fmt(rev):>12}  fcf {_fmt(fcf):>12}  pv {_fmt(pv):>12}")
    print(f"  terminal value            {_fmt(res.detail['tv']):>12}  pv {_fmt(res.detail['pv_tv']):>12}")
    print(f"  Enterprise value          {_fmt(res.detail['ev']):>12}")
    print()
    print(f"DCF   (all FCF, owner ceiling): {_fmt(res.dcf_equity):>14}  => {_fmt(res.dcf_per_share)}/share")
    print(f"DDM   (dividend floor):         {_fmt(res.ddm_value):>14}  => {_fmt(res.ddm_per_share)}/share"
          + (f"   (payout {res.payout_pct:.0f}% of profit)" if res.payout_pct else ""))
    if res.market_cap:
        print(f"Market(traded price):           {_fmt(res.market_cap):>14}  => {_fmt(res.market_per_share)}/share")
    print(f"\nFair value of a MINORITY share: {_fmt(res.fair_minority_low)} – {_fmt(res.fair_minority_high)} "
          f"(dividend floor → DCF ceiling; a dividend cut pulls it toward the floor).")


def _self_test():
    """Reproduce the bank's original GEX IPO memo to prove the model is correct."""
    d = _dcf(1_850_000, 4_311_630, 0, WACC_MONTHLY, NWC_PCT, CAPEX_PCT,
             GROWTH_MONTHLY, TERMINAL_G, t0_rev=2_589_940, t0_capex_abs=135_000)
    assert abs(d["ev"] - 28_991_104) < 5_000, d["ev"]
    assert abs(d["equity"] - 33_302_734) < 5_000, d["equity"]
    print(f"[self-test OK] reproduces memo: EV ${d['ev']/1e6:.2f}M, equity ${d['equity']/1e6:.2f}M")


def load_from_db(db_path, market_id, baseline_months=3):
    """Pull the inputs the database DOES have for a listed market:
       shares outstanding, current lowest ask (traded price), holder count, and a
       CSN revenue/profit proxy from recent csn_history months.
    NOTE: CSN only tracks SHOP sales — total company revenue (land flips, futures,
    services) is higher and lives in your earnings sheet, so pass `fwd_rev` to
    override for accuracy. Cash and dividends aren't in the DB either — pass them."""
    import sqlite3
    con = sqlite3.connect(db_path); con.row_factory = sqlite3.Row
    sh = con.execute("SELECT * FROM market_shares WHERE market_id=?", (market_id,)).fetchone()
    shares = float(sh["shares_outstanding"]) if sh else None
    share_price = float(sh["share_price"]) if sh else None
    asks = [float(r["limit_price"]) for r in con.execute(
        "SELECT limit_price FROM stock_limit_orders WHERE market_id=? AND status='open' "
        "AND lower(side)='sell'", (market_id,))]
    traded = (min(asks) if asks else None) or share_price
    holders = con.execute("SELECT COUNT(*) c FROM stock_holdings WHERE market_id=? AND shares>0",
                          (market_id,)).fetchone()["c"]
    rows = con.execute("SELECT month, income, net FROM csn_history WHERE market_id=? "
                       "ORDER BY month DESC LIMIT ?", (market_id, baseline_months)).fetchall()
    con.close()
    csn_rev = (sum(float(r["income"]) for r in rows) / len(rows)) if rows else None
    csn_profit = (sum(float(r["net"]) for r in rows) / len(rows)) if rows else None
    return dict(shares=shares, share_price=share_price, traded_price=traded, holders=holders,
                csn_rev=csn_rev, csn_profit=csn_profit,
                csn_months=[dict(r) for r in rows])


def load_financials_from_db(db_path, market_id, months=3):
    """Read owner-entered TOTAL financials from the optional `company_financials`
    table (revenue/profit/dividend/cash — the numbers CSN can't give). Returns
    {fwd_rev, profit, dividend, cash} using the trailing `months` avg for
    revenue/profit and the latest row for dividend/cash, or None if the table or
    data isn't there yet."""
    import sqlite3
    con = sqlite3.connect(db_path); con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT * FROM company_financials WHERE market_id=? ORDER BY month DESC LIMIT ?",
            (market_id, months)).fetchall()
    except sqlite3.OperationalError:
        con.close(); return None            # table not created yet
    con.close()
    if not rows:
        return None
    rev = sum(float(r["revenue"] or 0) for r in rows) / len(rows)
    prof = sum(float(r["profit"] or 0) for r in rows) / len(rows)
    latest = rows[0]
    cash = next((float(r["cash"]) for r in rows if r["cash"] is not None), None)
    return dict(fwd_rev=rev, profit=prof, dividend=float(latest["dividend"] or 0), cash=cash)


def value_from_db(db_path, market_id, *, cash=None, dividend_mo=None, fwd_rev=None,
                  monthly_profit=None, **kw):
    """Value a listed market. Pulls shares/price/holders from the DB, and prefers the
    `company_financials` table for revenue/profit/dividend/cash (owner-entered totals),
    falling back to a CSN shop-sales proxy and then to explicit args. Explicit args
    always win when provided."""
    db = load_from_db(db_path, market_id)
    if not db["shares"]:
        raise SystemExit(f"Market '{market_id}' has no share listing in {db_path} "
                         f"(run /market go_public first, or check the id).")
    fin = load_financials_from_db(db_path, market_id) or {}
    rev  = fwd_rev        if fwd_rev is not None        else fin.get("fwd_rev",  db["csn_rev"])
    prof = monthly_profit if monthly_profit is not None else fin.get("profit",   db["csn_profit"])
    div  = dividend_mo    if dividend_mo is not None    else fin.get("dividend", None)
    csh  = cash           if cash is not None           else fin.get("cash",     4_311_630)
    if rev is None:
        raise SystemExit("No revenue available — enter it with /set_financials or pass fwd_rev.")
    res = value(rev, csh, db["shares"], dividend_mo=div, monthly_profit=prof,
                traded_price=db["traded_price"], **kw)
    return res, db


if __name__ == "__main__":
    import argparse
    _self_test(); print()
    ap = argparse.ArgumentParser(description="Value a company (DB mode or manual).")
    ap.add_argument("--db", help="path to restocker.db (enables DB mode)")
    ap.add_argument("--market", help="market_id to value (with --db)")
    ap.add_argument("--cash", type=float, default=4_311_630)
    ap.add_argument("--dividend", type=float, default=None, help="monthly dividend run-rate")
    ap.add_argument("--revenue", type=float, default=None, help="forward monthly revenue (from earnings sheet)")
    ap.add_argument("--profit", type=float, default=None, help="monthly profit (for payout ratio)")
    a = ap.parse_args()
    if a.db and a.market:
        res, db = value_from_db(a.db, a.market, cash=a.cash, dividend_mo=a.dividend,
                                fwd_rev=a.revenue, monthly_profit=a.profit)
        print(f"[from DB] {a.market}: {db['shares']:.0f} shares, {db['holders']} holders, "
              f"traded {db['traded_price']}, CSN rev proxy {db['csn_rev']}")
        if a.revenue is None:
            print("  ⚠ used CSN shop-sales as revenue proxy — pass --revenue from your earnings "
                  "sheet for a true figure (total revenue > CSN).")
        report(res, db["shares"], a.market)
    else:
        # ── Manual mode: edit and run `python value_company.py` ──────────────
        res = value(fwd_rev=4_000_000, cash=4_311_630, shares=40_000, debt=0,
                    dividend_mo=171_000, monthly_profit=2_500_000, traded_price=1899)
        report(res, 40_000, "GEX (Global Exports)")
