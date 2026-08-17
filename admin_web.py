"""
admin_web.py — the owner's "god mode" for the V Tech site.

WHAT THIS IS
────────────
John owns the server, the bot, the coin wallet and this website. He asked for one
place to see and run everything, instead of scattered slash commands and me running
scripts for him. This is that place, and it is deliberately powerful. It is also the
one surface on the site with a capability it will NOT grow.

THE ONE THING GOD MODE DOES NOT DO — WRITE AS SOMEBODY ELSE
──────────────────────────────────────────────────────────
There is no "post as this player", no trade in their name, no row authored by them,
no holding of their session. The reason is written where someone will hit it, in
`vt_web_shell.refuse_if_impersonating`, and it is not distrust: the site's messaging
and history are worth something for exactly one reason — nobody can author the other
side of them. An owner-writes-as-anyone mode makes every record answerable with "the
owner could have written that", including the true ones, and spends the credibility
of every record he owns to buy a capability he does not need. He owns his alts and
signs into them normally; the dev login below covers testing.

So this module gives him three things and no fourth:

  1. READ-ONLY VIEW-AS. Enter as any player by id or handle; the site then renders
     THEIR real pages (inbox, holdings, ledger, threads) with a banner that cannot be
     missed or dismissed without exiting. It is structurally read-only: the refusal
     lives in the request path (`vt_web_shell.refuse_if_impersonating`, wired into the
     two POST chokepoints), not in a hidden button. Every entry, exit, page viewed and
     refused write is an append-only audit row the SUBJECT can read — an audit trail
     only the auditor can see is not a check on the auditor.

  2. THE OWNER CONSOLE. The levers that exist today, in one view, with the figures a
     lever will move shown beside its button (the house rule; `cogs/rollback.py` is the
     precedent). The `realestate:bidding_frozen` kill switch is a live control; markets,
     prices, the P/E multiplier, treasury, share issuance, dividends and config are
     surfaced with their real numbers.

  3. DEV LOGIN (local only, off by default). Become any user id for testing, behind
     three independent gates — see `h_dev_login`.

IDENTITY, AS EVERYWHERE ELSE ON THIS SITE
─────────────────────────────────────────
`shell.session_user` is the only whoami and it always returns the REAL staff member.
View-as changes what is rendered, never who the session is. Every write path reads the
real session and refuses on the view-as flag. A body-supplied id is ignored and alarmed
(`hub_web._scan_body_identity` / `hub_attack_log`), exactly as the money routes do it.
"""

from __future__ import annotations

import html
import logging
import os
import time
from typing import Any, Optional

try:
    from aiohttp import web
except Exception:  # pragma: no cover - aiohttp absent in a bare import check
    web = None  # type: ignore

import vt_web_shell as shell

log = logging.getLogger("admin_web")

ADMIN_VERSION = "1.0"

#: Endpoint name the kill-switch claims under (see `shell.money_post`).
FREEZE_ENDPOINT = "admin/freeze"

#: The kill switch. Documented across the land-escrow plan as "the universal rollback
#: primitive — it stops new money entering the system in seconds, without a deploy".
FREEZE_KEY = "realestate:bidding_frozen"


# ══════════════════════════════════════════════════════════════════════════
# Lazy core handles — same convention as `messages_web`, so this imports and is
# testable without the bot present.
# ══════════════════════════════════════════════════════════════════════════

def _core_db():
    import Restocker_db as _db
    return _db


# ══════════════════════════════════════════════════════════════════════════
# Names — one source, the same `stock_names.yml` the rest of the site reads
# ══════════════════════════════════════════════════════════════════════════

_NAMES_CACHE: dict = {}
_NAMES_AT = 0.0


def _names() -> dict:
    global _NAMES_CACHE, _NAMES_AT
    if time.time() - _NAMES_AT < 60:
        return _NAMES_CACHE
    out = {}
    try:
        import Restocker_web as _rw
        out = _rw._load_data_yaml("stock_names.yml", {}) or {}
    except Exception:
        out = {}
    _NAMES_CACHE, _NAMES_AT = out, time.time()
    return out


def _display_name(uid: str) -> str:
    uid = str(uid or "")
    if not uid:
        return "—"
    nm = _names().get(uid)
    nm = str(nm).strip() if nm else ""
    return nm or "Unnamed player"


def _has_wallet(uid: str) -> bool:
    try:
        with _core_db().db() as conn:
            row = conn.execute("SELECT 1 FROM balances WHERE user_id = ?",
                               (str(uid),)).fetchone()
        return row is not None
    except Exception:
        return False


def _resolve_subject(q: str) -> Optional[dict]:
    """`{user_id, name}` for a view-as target given a raw id OR a handle, else None.

    An id is anything with a wallet or a known name; a handle is matched against the
    display-name map case-insensitively. Refusing to resolve is the safe direction —
    a bad target simply cannot be entered."""
    q = str(q or "").strip()
    if not q:
        return None
    # A direct id: it holds a wallet, or we have a name on file for it.
    if _has_wallet(q) or q in _names():
        return {"user_id": q, "name": _display_name(q)}
    # A handle: reverse the name map. First exact (case-insensitive), then unique prefix.
    lo = q.lower()
    exact = [uid for uid, nm in _names().items() if str(nm).strip().lower() == lo]
    if len(exact) == 1:
        return {"user_id": str(exact[0]), "name": _display_name(exact[0])}
    return None


# ══════════════════════════════════════════════════════════════════════════
# Staff gates — 401 anonymous, 403 a normal player, on every admin surface.
# The page must 403 for a non-staff player; an unlinked nav tab is not the gate.
# ══════════════════════════════════════════════════════════════════════════

def _forbidden_page(request) -> Any:
    body = ('<div class="page-head"><div><h1>Owner console</h1>'
            '<div class="page-sub">This area is staff only.</div></div></div>'
            '<div class="adm-empty">You do not have access to this page.</div>')
    shell.set_page_ctx(None, False)
    resp = shell.page("Owner console", "admin", body,
                      "loadMe && loadMe().then(()=>renderStrip && renderStrip());")
    resp.set_status(403)
    return resp


def _require_staff_page(request):
    """`(sess, None)` for a staff GET page; `(None, refusal)` otherwise.

    Uses the REAL session and does NOT swap to a view-as target — the console is the
    staff member's own tool. It still shows the view-as banner (so an owner mid-view
    can always get out from here) and lights the Owner nav tab."""
    sess = shell.session_user(request)
    if not sess:
        shell.set_page_ctx(None, False)
        return None, shell.login_page(request)
    if not shell.is_staff(sess):
        return None, _forbidden_page(request)
    va = shell.active_view_as(str(sess["user_id"]))
    shell.set_page_ctx(
        ({"real_id": str(sess["user_id"]),
          "real_name": sess.get("name") or str(sess["user_id"]),
          "target_id": str(va["target_id"]),
          "target_name": va.get("target_name") or str(va["target_id"])} if va else None),
        True)
    return sess, None


def _require_staff_json(request, need_csrf: bool = True):
    """`(sess, None)` for a staff JSON/POST route; `(None, refusal)` otherwise.

    Body-supplied identity is alarmed and ignored on every mutating admin route,
    exactly as the money routes do it — a `user_id` in the body is never trusted."""
    sess = shell.session_user(request)
    if not sess:
        return None, shell.json_err("not_logged_in", "Log in first.", 401)
    if not shell.is_staff(sess):
        return None, shell.json_err("forbidden", "This action is staff only.", 403)
    if need_csrf and not shell.csrf_ok(request):
        return None, shell.json_err("bad_csrf", "Bad or missing CSRF token. Reload.", 403)
    return sess, None


def _note_identity(request, body: Any, sess: dict, endpoint: str) -> None:
    try:
        import hub_web
        hub_web._scan_body_identity(body if isinstance(body, dict) else {},
                                    request, str(sess.get("user_id") or ""), endpoint)
    except Exception:  # pragma: no cover
        pass


# ══════════════════════════════════════════════════════════════════════════
# Console reads — the real levers, with their real figures
# ══════════════════════════════════════════════════════════════════════════

def _markets_overview() -> list:
    """Every listed company with the numbers the owner steers: shares outstanding,
    the P/E multiplier, the last share price, treasury, and the dividend override."""
    try:
        with _core_db().db() as conn:
            rows = conn.execute(
                "SELECT market_id, active, shares_outstanding, pe_multiplier, share_price, "
                "       treasury_coins, dividend_pct, last_dividend_month "
                "  FROM market_shares ORDER BY treasury_coins DESC").fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("[admin] markets overview read failed")
        return []


def _treasury_overview() -> dict:
    """Where the coin is. Total in wallets, the top holders, and the platform float."""
    out = {"total_coins": 0.0, "wallets": 0, "top": [], "platform": None}
    try:
        with _core_db().db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(coins),0) AS s FROM balances").fetchone()
            out["wallets"] = int(row["n"] or 0)
            out["total_coins"] = float(row["s"] or 0)
            out["top"] = [dict(r) for r in conn.execute(
                "SELECT user_id, coins FROM balances ORDER BY coins DESC LIMIT 8").fetchall()]
            try:
                pr = conn.execute("SELECT COALESCE(SUM(balance),0) AS b "
                                  "FROM platform_balance").fetchone()
                out["platform"] = float(pr["b"] or 0) if pr else None
            except Exception:
                out["platform"] = None
    except Exception:
        log.exception("[admin] treasury overview read failed")
    return out


def _freeze_state() -> dict:
    """The kill switch, AND the figures throwing it will move: how many auctions are
    live and how much coin is currently escrowed in their top bids. Those are the
    numbers a freeze protects, and the house rule says they belong beside the button."""
    db = _core_db()
    frozen = str(db.get_config(FREEZE_KEY) or "").strip().lower() in ("1", "true", "on", "yes")
    live = 0
    exposed = 0.0
    try:
        with db.db() as conn:
            r = conn.execute(
                "SELECT COUNT(*) AS n FROM land_listings WHERE status = 'active'").fetchone()
            live = int(r["n"] or 0) if r else 0
            # Highest current bid per active auction = coin in play the switch guards.
            r2 = conn.execute(
                "SELECT COALESCE(SUM(mx),0) AS s FROM ("
                "  SELECT MAX(b.amount) AS mx FROM land_bids b "
                "  JOIN land_listings l ON l.id = b.listing_id "
                "  WHERE l.status = 'active' GROUP BY b.listing_id)").fetchone()
            exposed = float(r2["s"] or 0) if r2 else 0.0
    except Exception:
        log.exception("[admin] freeze-state figures read failed")
    return {"frozen": frozen, "live_auctions": live, "coin_in_bids": exposed}


def _config_levers() -> list:
    """The config keys that are levers, with their live values. Read-only here; the
    only config the console WRITES is the kill switch, which has its own control."""
    keys = (FREEZE_KEY, "exchange_insurance_fund", "realestate:deals_channel",
            "web_staff_ids", "earnings_hidden_markets")
    db = _core_db()
    out = []
    for k in keys:
        try:
            out.append({"key": k, "value": db.get_config(k)})
        except Exception:
            out.append({"key": k, "value": None})
    return out


# ══════════════════════════════════════════════════════════════════════════
# Rendering — house theme, zero radius, mono figures, tables for rows
# ══════════════════════════════════════════════════════════════════════════

def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _coins(v: Any) -> str:
    try:
        return f"{int(round(float(v or 0))):,}"
    except (TypeError, ValueError):
        return "0"


_CSS = """
<style>
.adm-grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.adm-tile{grid-column:span 12;border:1px solid var(--border);background:var(--surface);padding:16px 18px}
.adm-tile.s6{grid-column:span 6}
.adm-h{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
  font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:10px}
.adm-h::after{content:"";flex:1;height:1px;background:var(--border)}
.adm-empty{padding:16px 0;color:var(--muted);font-size:12px}
.adm-kill{display:flex;align-items:center;justify-content:space-between;gap:16px;
  border:1px solid var(--border-strong);background:var(--panel2);padding:14px 16px}
.adm-kill .figs{font-size:12px;color:var(--text-body);line-height:1.6}
.adm-kill .figs b{color:var(--money-held);font-family:var(--font-data);font-variant-numeric:tabular-nums}
.adm-state{font-family:var(--font-data);font-size:11px;font-weight:600;text-transform:uppercase;
  letter-spacing:.06em;padding:3px 8px}
.adm-state.on{background:var(--red);color:#000}
.adm-state.off{background:var(--panel2);color:var(--muted);border:1px solid var(--border)}
.adm-form{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin-top:4px}
.adm-form input{background:var(--panel2);border:1px solid var(--border);color:var(--text);
  font-family:var(--font-data);font-size:13px;padding:8px 10px;min-width:240px}
.adm-form input:focus{outline:none;border-color:var(--border-strong)}
.adm-err{margin-top:9px;font-size:12px;color:var(--red)}
.adm-ok{margin-top:9px;font-size:12px;color:var(--accent)}
.adm-num{font-family:var(--font-data);font-variant-numeric:tabular-nums slashed-zero;text-align:right}
.adm-sub{font-size:12px;color:var(--text-body);max-width:64ch;line-height:1.55}
</style>
"""


def _kill_tile(fr: dict, key_on: str, key_off: str) -> str:
    state = ('<span class="adm-state on">FROZEN</span>' if fr["frozen"]
             else '<span class="adm-state off">live</span>')
    # The house rule: the figures the switch will move, in the same view as the button.
    figs = (f'<div class="figs">{fr["live_auctions"]} live auction'
            f'{"" if fr["live_auctions"] == 1 else "s"} · '
            f'<b>{_coins(fr["coin_in_bids"])}c</b> escrowed in current top bids</div>')
    if fr["frozen"]:
        btn = (f'<button class="btn" onclick="setFreeze(false, this)" '
               f'data-key="{esc(key_off)}">Unfreeze bidding</button>')
        note = ('Bidding is frozen: new bids and instant-buys are refused server-side. '
                'Unfreezing re-opens the money path above.')
    else:
        btn = (f'<button class="btn" style="background:var(--red)" '
               f'onclick="setFreeze(true, this)" data-key="{esc(key_on)}">Freeze all bidding</button>')
        note = ('Freezing stops all new bids and instant-buys in seconds, without a deploy. '
                'The coin already escrowed above stays put; nothing new enters.')
    return f"""
<div class="adm-tile">
  <div class="adm-h">Kill switch · {esc(FREEZE_KEY)}</div>
  <div class="adm-kill">
    <div>{state} {figs}</div>
    <div>{btn}</div>
  </div>
  <div class="adm-sub" style="margin-top:10px">{note}</div>
  <div class="adm-err" id="freezeErr"></div>
  <div class="adm-ok" id="freezeOk"></div>
</div>"""


def _markets_tile(rows: list) -> str:
    if not rows:
        body = '<div class="adm-empty">No listed companies.</div>'
    else:
        trs = []
        for m in rows:
            div = "—" if m.get("dividend_pct") is None else f'{float(m["dividend_pct"]):g}%'
            trs.append(
                f'<tr><td>{esc(m["market_id"])}</td>'
                f'<td>{"listed" if m.get("active") else "delisted"}</td>'
                f'<td class="adm-num">{_coins(m.get("shares_outstanding"))}</td>'
                f'<td class="adm-num">{float(m.get("pe_multiplier") or 0):g}×</td>'
                f'<td class="adm-num">{_coins(m.get("share_price"))}c</td>'
                f'<td class="adm-num">{_coins(m.get("treasury_coins"))}c</td>'
                f'<td class="adm-num">{div}</td></tr>')
        body = ('<table><thead><tr><th>Company</th><th>State</th><th class="adm-num">Shares</th>'
                '<th class="adm-num">P/E</th><th class="adm-num">Price</th>'
                '<th class="adm-num">Treasury</th><th class="adm-num">Dividend</th></tr></thead>'
                '<tbody>' + "".join(trs) + '</tbody></table>')
    return f'<div class="adm-tile"><div class="adm-h">Markets · prices · P/E · share issuance · dividends</div>{body}</div>'


def _treasury_tile(t: dict) -> str:
    plat = "" if t["platform"] is None else f' · platform float <b>{_coins(t["platform"])}c</b>'
    trs = "".join(
        f'<tr><td>{esc(_display_name(r["user_id"]))}</td>'
        f'<td style="color:var(--faint)">{esc(r["user_id"])}</td>'
        f'<td class="adm-num">{_coins(r["coins"])}c</td></tr>'
        for r in t["top"])
    table = ('<table><thead><tr><th>Holder</th><th>Id</th><th class="adm-num">Coins</th>'
             '</tr></thead><tbody>' + trs + '</tbody></table>') if trs else \
            '<div class="adm-empty">No wallets.</div>'
    return (f'<div class="adm-tile"><div class="adm-h">Treasury · coin in the economy</div>'
            f'<div class="adm-sub" style="margin-bottom:12px">{t["wallets"]} wallets · '
            f'<b style="font-family:var(--font-data)">{_coins(t["total_coins"])}c</b> in circulation{plat}</div>'
            f'{table}</div>')


def _config_tile(levers: list) -> str:
    trs = "".join(
        f'<tr><td>{esc(l["key"])}</td>'
        f'<td style="color:var(--text-body)">{esc(l["value"]) if l["value"] is not None else "<span style=color:var(--faint)>unset</span>"}</td></tr>'
        for l in levers)
    return (f'<div class="adm-tile"><div class="adm-h">Config levers</div>'
            f'<table><thead><tr><th>Key</th><th>Value</th></tr></thead>'
            f'<tbody>{trs}</tbody></table></div>')


def _view_as_tile(sess: dict, va: Optional[dict]) -> str:
    if va:
        inner = (f'<div class="adm-sub">Currently viewing as '
                 f'<b style="color:var(--nether)">{esc(va.get("target_name") or va["target_id"])}</b> '
                 f'<span style="color:var(--faint)">{esc(va["target_id"])}</span>. '
                 f'The banner at the top of every page carries the exit.</div>'
                 f'<div class="adm-form"><button class="btn ghost" onclick="exitViewAs()">'
                 f'Exit view-as</button></div>')
    else:
        inner = (
            '<div class="adm-sub">Enter a player by id or handle to see the site through '
            'their eyes — their inbox, holdings and ledger, exactly as they see them. '
            'It is strictly read-only and every page you view is recorded where they can '
            'see it.</div>'
            '<div class="adm-form">'
            '<input id="vaSubject" placeholder="user id or handle" autocomplete="off">'
            '<button class="btn" onclick="enterViewAs()">Enter view-as</button></div>'
            '<div class="adm-err" id="vaErr"></div>')
    return f'<div class="adm-tile"><div class="adm-h">Read-only view-as</div>{inner}</div>'


def _audit_tile(rows: list) -> str:
    if not rows:
        body = '<div class="adm-empty">No admin actions recorded yet.</div>'
    else:
        trs = []
        for r in rows[:40]:
            trs.append(
                f'<tr><td title="{esc(shell.human_date(r["ts"]) if hasattr(shell,"human_date") else "")}">'
                f'{esc(_ago(r["ts"]))}</td>'
                f'<td>{esc(r["action"])}</td>'
                f'<td>{esc(_display_name(r["subject_id"])) if r["subject_id"] else "—"}</td>'
                f'<td style="color:var(--text-body)">{esc(r["detail"])}</td></tr>')
        body = ('<table><thead><tr><th>When</th><th>Action</th><th>Subject</th>'
                '<th>Detail</th></tr></thead><tbody>' + "".join(trs) + '</tbody></table>')
    return f'<div class="adm-tile"><div class="adm-h">Audit trail · your actions</div>{body}</div>'


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _ago(ts: Any) -> str:
    try:
        t = float(ts or 0)
    except (TypeError, ValueError):
        return "—"
    if t <= 0:
        return "—"
    d = time.time() - t
    if d < 45:
        return "just now"
    if d < 3600:
        return f"{int(round(d / 60))} min ago"
    if d < 86400:
        return f"{int(d // 3600)} hours ago"
    if d < 604800:
        return f"{int(d // 86400)} days ago"
    tm = time.gmtime(t)
    return f"{tm.tm_mday} {_MONTHS[tm.tm_mon - 1]} {tm.tm_year}"


def _console_body(sess: dict) -> str:
    uid = str(sess["user_id"])
    va = shell.active_view_as(uid)
    fr = _freeze_state()
    key_on = shell.mint_form_key(uid, "admin:freeze:on")
    key_off = shell.mint_form_key(uid, "admin:freeze:off")
    return f"""{_CSS}
<div class="page-head">
  <div>
    <h1>Owner console</h1>
    <div class="page-sub">Everything you can see and run, in one place. Anything that
    moves coin shows the figures it will move, beside the button. Writing as another
    account is the one thing this does not do — the record only means something because
    nobody can author the other side of it.</div>
  </div>
</div>
<div class="adm-grid">
  {_view_as_tile(sess, va)}
  {_kill_tile(fr, key_on, key_off)}
  {_markets_tile(_markets_overview())}
  {_treasury_tile(_treasury_overview())}
  {_config_tile(_config_levers())}
  {_audit_tile(shell.audit_by_actor(uid, 60))}
</div>
"""


_CONSOLE_JS = r"""
loadMe().then(() => { if(window.renderStrip) renderStrip(); });

async function enterViewAs(){
  const inp = document.getElementById('vaSubject');
  const err = document.getElementById('vaErr');
  err.textContent = '';
  const subject = (inp.value || '').trim();
  if(!subject){ err.textContent = 'Enter a user id or handle.'; return; }
  const r = await post('/api/admin/view-as/enter', {subject});
  if(r && r.ok){ location.href = r.landing || '/messages'; return; }
  err.textContent = (r && r.error) || 'Could not enter view-as.';
}

async function exitViewAs(){
  await post('/api/admin/view-as/exit', {});
  location.reload();
}

async function setFreeze(on, btn){
  const err = document.getElementById('freezeErr');
  const ok  = document.getElementById('freezeOk');
  err.textContent = ''; ok.textContent = '';
  if(btn.disabled) return; btn.disabled = true;
  const r = await post('/api/admin/freeze', {freeze: on, idempotency_key: btn.dataset.key});
  if(r && r.ok){ ok.textContent = r.message || 'Done.'; setTimeout(()=>location.reload(), 500); return; }
  err.textContent = (r && r.error) || 'The change was not applied.';
  btn.disabled = false;
}
"""


# ══════════════════════════════════════════════════════════════════════════
# Routes — pages
# ══════════════════════════════════════════════════════════════════════════

async def h_console(request):
    """`GET /admin` — the owner console. Staff only: 401 anon, 403 a normal player."""
    sess, refusal = _require_staff_page(request)
    if refusal is not None:
        return refusal
    return shell.page("Owner console", "admin", _console_body(sess), _CONSOLE_JS)


# ══════════════════════════════════════════════════════════════════════════
# Routes — view-as control plane (NOT through the write chokepoint on purpose:
# exit must work WHILE in view-as, and entering switches the target. These are the
# owner's controls over the flag, not economy writes, so `refuse_if_impersonating`
# does not gate them — but they are staff-gated, CSRF-checked and fully audited.)
# ══════════════════════════════════════════════════════════════════════════

async def h_view_as_enter(request):
    """`POST /api/admin/view-as/enter` — begin viewing the site as a player."""
    sess, refusal = _require_staff_json(request)
    if refusal is not None:
        return refusal
    body = await shell.read_json(request)
    _note_identity(request, body, sess, "admin/view-as/enter")
    subject = str(body.get("subject") or body.get("target") or "").strip()
    who = _resolve_subject(subject)
    if not who:
        return shell.json_err(
            "no_such_subject",
            "No player found for that id or handle. Enter a Discord id, or a name "
            "exactly as it appears on the site.", 404)
    if str(who["user_id"]) == str(sess["user_id"]):
        return shell.json_err("self_view", "You are already yourself.", 400)
    shell.enter_view_as(str(sess["user_id"]), str(who["user_id"]), who["name"],
                        shell.client_ip(request))
    return shell.json_ok(target_id=who["user_id"], target_name=who["name"],
                         landing="/messages",
                         message=f"Now viewing as {who['name']} — read only.")


async def h_view_as_exit(request):
    """`POST /api/admin/view-as/exit` — stop viewing as anyone. Must work WHILE in
    view-as, so it is deliberately not behind the write chokepoint."""
    sess, refusal = _require_staff_json(request)
    if refusal is not None:
        return refusal
    was = shell.exit_view_as(str(sess["user_id"]), shell.client_ip(request))
    return shell.json_ok(exited=bool(was), was=was)


# ══════════════════════════════════════════════════════════════════════════
# Routes — the kill switch (THROUGH the write chokepoint, so it is refused while
# the acting staff member is in view-as, like every other economy write).
# ══════════════════════════════════════════════════════════════════════════

def _freeze_purpose(body: dict) -> str:
    on = bool((body or {}).get("freeze"))
    return f"admin:freeze:{'on' if on else 'off'}"


async def _do_freeze(sess, body, key):
    """Set or clear the bidding kill switch. Staff has already been proven by the
    route; `money_post` has proven session, CSRF, key subject and the claim."""
    uid = str(sess["user_id"])
    on = bool((body or {}).get("freeze"))
    before = _freeze_state()
    _core_db().set_config(FREEZE_KEY, "1" if on else "0")
    shell.audit_admin(uid, "", "freeze_bidding" if on else "unfreeze_bidding",
                      f"{FREEZE_KEY} -> {'1' if on else '0'} "
                      f"({before['live_auctions']} live, {int(before['coin_in_bids'])}c in bids)",
                      "")
    return 200, {"ok": True, "frozen": on,
                 "message": ("Bidding frozen — new bids and instant-buys are now refused."
                             if on else "Bidding unfrozen — the money path is open again.")}


async def h_freeze(request):
    """`POST /api/admin/freeze` — throw or clear the kill switch. Staff only, and
    routed through `money_post`, so a staff member in view-as is refused here too."""
    sess = shell.session_user(request)
    if not sess:
        return shell.json_err("not_logged_in", "Log in first.", 401)
    if not shell.is_staff(sess):
        return shell.json_err("forbidden", "This action is staff only.", 403)
    body = await shell.read_json(request)
    _note_identity(request, body, sess, FREEZE_ENDPOINT)
    return await shell.money_post(request, FREEZE_ENDPOINT, _freeze_purpose, _do_freeze)


# ══════════════════════════════════════════════════════════════════════════
# The subject's own view of the audit — an auditor who cannot be audited is not
# checked. Any logged-in user may read the rows ABOUT THEMSELVES, and only those.
# ══════════════════════════════════════════════════════════════════════════

async def h_my_observers(request):
    """`GET /api/admin/observed-me` — every admin action recorded about the caller.
    Not staff-gated: it returns only rows where the caller is the SUBJECT."""
    sess = shell.session_user(request)
    if not sess:
        return shell.json_err("not_logged_in", "Log in first.", 401)
    uid = str(sess["user_id"])
    rows = shell.audit_for_subject(uid, 200)
    out = [{"when": _ago(r["ts"]), "action": r["action"], "detail": r["detail"],
            "by_staff": r["actor_id"] == uid} for r in rows]
    return shell.json_ok(observed=out, count=len(out))


# ══════════════════════════════════════════════════════════════════════════
# Dev login — LOCAL ONLY, OFF BY DEFAULT. Three independent gates, all required.
# ══════════════════════════════════════════════════════════════════════════

#: Test seam, mirroring `shell.set_session_provider`: the production minter writes a
#: real session into Restocker_web's store; a test installs a minter over the same
#: fake cookie map its session provider reads. Never touched in production.
_DEV_MINTER = None


def set_dev_session_minter(fn) -> None:
    global _DEV_MINTER
    _DEV_MINTER = fn


def _mint_dev_session(uid: str, name: str):
    if _DEV_MINTER is not None:
        return _DEV_MINTER(uid, name)
    import hub_web
    return hub_web._mint_session(uid, name)


def _set_dev_cookie(resp, token: str) -> None:
    try:
        import hub_web
        hub_web._set_session_cookie(resp, token)
    except Exception:  # pragma: no cover
        resp.set_cookie("vtm_sess", token, httponly=True, samesite="Lax", path="/")


#: What makes a box "loopback": the socket the request arrived on is bound to a
#: loopback address, NOT merely that the client claims to be local. `0.0.0.0` and
#: `::` are bound to every interface and are therefore reachable from the network —
#: they are NOT loopback and must fail this gate.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _loopback_bound(request) -> bool:
    """True only if this request came in on a socket bound to a loopback address.

    Reads the SERVER side of the connection (`sockname`), not the client side: a
    request that reached a public-facing bind fails this even if it claims to be
    from localhost, which is the whole point — the dev login must be unreachable
    from the internet, not merely un-advertised."""
    try:
        sock = request.transport.get_extra_info("sockname")
    except Exception:
        return False
    if not sock:
        return False
    host = str(sock[0]) if isinstance(sock, (tuple, list)) and sock else ""
    if host.startswith("127."):
        return True
    return host in _LOOPBACK_HOSTS


def _dev_env_marker() -> bool:
    """The non-production marker. It is a POSITIVE opt-in — `VTECH_ENV` must be set
    to one of dev/test/local — chosen over "absence of a production flag" precisely
    so it CANNOT be satisfied by accident on the Wisp box: a real deploy leaves
    `VTECH_ENV` unset or "production", and an unset marker fails closed. Even a
    stray `VTECH_DEV_LOGIN=1` in production is stopped independently by this gate
    and by the loopback gate — three locks, each of which fails shut on its own."""
    return os.getenv("VTECH_ENV", "").strip().lower() in ("dev", "test", "local")


def _dev_login_enabled() -> bool:
    return os.getenv("VTECH_DEV_LOGIN", "").strip() == "1"


async def h_dev_login(request):
    """`POST /api/admin/dev-login` {user_id} — become any user id, FOR TESTING.

    Three independent gates, ALL required, each of which refuses loudly and logs on
    its own so no single misconfiguration opens the door:

      1. `VTECH_DEV_LOGIN=1`         — an explicit opt-in, off by default.
      2. the server is bound to loopback (`_loopback_bound`) — unreachable from the
         internet, checked on the socket, not on a client claim.
      3. a non-production marker (`_dev_env_marker`) — `VTECH_ENV` in {dev,test,local}.

    This is what makes testing the site pleasant; it is also the thing that must never
    be reachable from production, so it fails shut on every gate and each refusal is a
    logged 403 naming the gate that was not met."""
    ip = shell.client_ip(request)
    if not _dev_login_enabled():
        log.error("[admin][DEV-LOGIN] REFUSED — VTECH_DEV_LOGIN is not set (ip=%s)", ip)
        return shell.json_err("dev_login_off",
                              "Dev login is disabled (VTECH_DEV_LOGIN).", 403)
    if not _loopback_bound(request):
        log.error("[admin][DEV-LOGIN] REFUSED — request did not arrive on a loopback "
                  "bind (ip=%s). Dev login is never served off-loopback.", ip)
        return shell.json_err("not_loopback",
                              "Dev login is only served on a loopback bind.", 403)
    if not _dev_env_marker():
        log.error("[admin][DEV-LOGIN] REFUSED — no non-production marker "
                  "(VTECH_ENV not in dev/test/local) (ip=%s)", ip)
        return shell.json_err("looks_like_production",
                              "Dev login refuses without a non-production marker.", 403)

    body = await shell.read_json(request)
    uid = str(body.get("user_id") or body.get("uid") or "").strip()
    if not uid:
        return shell.json_err("no_user_id", "Pass a user_id to become.", 400)
    name = _display_name(uid)
    try:
        token = _mint_dev_session(uid, name)
    except Exception as e:
        log.exception("[admin][DEV-LOGIN] session mint failed: %s", e)
        return shell.json_err("mint_failed", "Could not mint a dev session.", 500)
    shell.audit_admin(uid, uid, "dev_login", f"dev login as {uid} (ip={ip})", ip)
    log.warning("[admin][DEV-LOGIN] granted a session as %s from %s", uid, ip)
    resp = shell.json_ok(user_id=uid, name=name, message=f"Logged in as {name} (dev).")
    _set_dev_cookie(resp, token)
    return resp


# ══════════════════════════════════════════════════════════════════════════
# Mount
# ══════════════════════════════════════════════════════════════════════════

def _register_with_hub() -> None:
    try:
        import hub_web
        hub_web.register_section("admin", "Owner", "/admin", order=90, staff_only=True)
    except Exception as e:  # pragma: no cover
        log.warning("[admin] could not register with the hub nav: %s", e)


def register_admin_routes(app) -> None:
    """Attach the owner console. Mirrors `messages_web.register_messages_routes`.

    The view-as control routes carry `name="admin_ctl_*"` so a test can DERIVE the
    set of economy-mutating routes (everything else) and assert each refuses under
    view-as, without a hand-maintained allow-list that would go stale."""
    if web is None:  # pragma: no cover
        log.warning("[admin] aiohttp unavailable — admin not registered.")
        return
    shell.register_shell_routes(app)
    _register_with_hub()
    app.router.add_get("/admin", h_console)
    # Control plane — the owner's levers over the view-as flag itself. Named so the
    # derived-route test can tell them apart from economy writes.
    app.router.add_post("/api/admin/view-as/enter", h_view_as_enter, name="admin_ctl_enter")
    app.router.add_post("/api/admin/view-as/exit", h_view_as_exit, name="admin_ctl_exit")
    app.router.add_post("/api/admin/dev-login", h_dev_login, name="admin_ctl_dev_login")
    # Economy write — routed through `money_post`, so it inherits the view-as refusal.
    app.router.add_post("/api/admin/freeze", h_freeze)
    # Read routes.
    app.router.add_get("/api/admin/observed-me", h_my_observers)
    log.info("[admin] v%s registered (console · view-as · kill switch · dev-login)",
             ADMIN_VERSION)
