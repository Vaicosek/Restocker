"""
hub_web.py — the V Tech Hub shell + the Markets section.

Mounted onto the EXISTING aiohttp app the same way bank_api is:

    import hub_web
    hub_web.register_hub_routes(app)

in `Restocker_web.start_webserver()`, in the try/except block immediately before
`web.AppRunner`. Nothing is appended to the flat `app.router.add_*` list.

WHAT THIS FILE IS
-----------------
1. THE SHELL every section shares: identity, the persistent money strip, the nav,
   the theme CSS, and idempotency-key minting/validation.
2. THE MARKETS SECTION: shops, the stock exchange, hives — read-only figures
   first, then buy/sell wired to the bot's OWN trade engine.

Other sections (Banking, Lands & Auctions, Ops, Mobile) call
`hub_web.register_section(...)` at import time and then use `current_user`,
`money_strip_html`, `page`, `mint_key` and `idempotent_post`. They must not
re-implement any of those. Gambling is out of scope for this economy: there is
no dice/coinflip/lottery/casino surface here and none may be added.

FIVE RULES THIS FILE ENFORCES, AND WHY
--------------------------------------
* IDENTITY IS THE SESSION, NEVER THE BODY. `current_user(request)` is the only
  way a handler learns who is asking. A user id in a request body is ignored and
  recorded in `hub_attack_log` — see `_scan_body_identity`.
* EVERY MONEY FORM CARRIES A SERVER-MINTED IDEMPOTENCY KEY. Minted when the
  confirm page renders, bound to (user, endpoint), claimed with a claim-first
  UPDATE, single-use. A replayed POST returns the ORIGINAL stored response.
* LIMITS AND BALANCES ARE RE-CHECKED AT SUBMIT TIME. The preview is indicative;
  the engine re-reads price, supply and wallet inside the bot loop.
* PREVIEW-THEN-CONFIRM WITH FIGURES. The confirm screen shows fill, total and
  the resulting balance next to the button, not on the screen before it.
* NO SECOND IMPLEMENTATION OF THE MATHS. Share pricing is
  `Restocker_main._quote_trade`; booking a trade is `Restocker_main._do_stock_trade`
  marshalled through `run_on_bot_loop`. This module never prices or books a
  trade itself.

THREADING
---------
The web server runs on its own OS thread and event loop. `_do_stock_trade`'s
supply check and its writes are not atomic; only sharing the bot's loop keeps a
web trade from interleaving with a Discord one. Every mutation here therefore
goes through `Restocker_main.run_on_bot_loop`.

DATA HONESTY
------------
Where the mockups show a figure with no real source, the panel is left out
rather than filled in. Concretely, as of this file: there is no savings table
and no loans table anywhere in Restocker's schema, so the strip's "Savings" and
"Net position" segments DO NOT RENDER until Osentar Bank registers a provider
via `register_money_provider`. `available` and `held` are real, from ledger_v2.
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import sqlite3
import time
import urllib.parse
from typing import Any, Callable, Optional

try:
    from aiohttp import web
except Exception:  # pragma: no cover - aiohttp is a hard dep of the web server
    web = None  # type: ignore

log = logging.getLogger("hub_web")

HUB_VERSION = "1.0"
HUB_PREFIX = "/hub"

SESSION_COOKIE = "vtm_sess"          # the SAME cookie Restocker_web already issues
OAUTH_STATE_COOKIE = "vtm_oauth"
SESSION_TTL = 30 * 24 * 3600

# Keys expire so an abandoned form can't be replayed a month later.
IDEM_TTL = 3600.0

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "").strip()
DISCORD_AUTHZ = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN = "https://discord.com/api/oauth2/token"
DISCORD_ME = "https://discord.com/api/users/@me"

# secure=True means the cookie is never set over plain http. That is correct
# behind the Cloudflare tunnel and fatal on http://localhost, which is why the
# escape hatch exists — it is opt-in and never on in production.
_INSECURE_COOKIES = os.getenv("HUB_INSECURE_COOKIES", "").strip() == "1"

MAX_SHARES_PER_TRADE = 1_000_000


# ══════════════════════════════════════════════════════════════════════════
# Lazy module handles. Imported inside functions so this file imports (and is
# testable) without the bot present, and so a stub can be installed in
# sys.modules before the first call.
# ══════════════════════════════════════════════════════════════════════════

def _web() -> Any:
    import Restocker_web as w
    return w


def _main() -> Any:
    import Restocker_main as m
    return m


def _db() -> Any:
    import Restocker_db as d
    return d


def _ledger() -> Any:
    import ledger_v2 as lg
    return lg


# ══════════════════════════════════════════════════════════════════════════
# Section registry — the nav is data, so a new section plugs in without
# editing this file.
# ══════════════════════════════════════════════════════════════════════════

_ICONS = {
    "markets": '<path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/>',
    "banking": '<rect x="3" y="8" width="18" height="12"/><path d="M3 8l9-5 9 5"/><path d="M8 20v-6M16 20v-6"/>',
    "lands": '<path d="M3 20h18"/><path d="M5 20V9l7-5 7 5v11"/><path d="M10 20v-6h4v6"/>',
    "messages": '<path d="M4 4h16v12H8l-4 4z"/><path d="M8 9h8M8 12h5"/>',
    "ops": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1"/>',
    "hub": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>',
}

_CHEVRON = '<path d="M6 9l6 6 6-6"/>'

_SECTIONS: list[dict] = []


def register_section(key: str, label: str, path: str, icon: str = "", order: int = 100,
                     staff_only: bool = False) -> None:
    """Add a nav entry. Called at import time by each section module.

    Idempotent on `key` so a module reloaded in a dev cycle doesn't double the
    nav. `icon` is an inline SVG path body (no emoji, ever) — omit it to use the
    built-in for a known key.

    `staff_only` sections (the Owner console) are withheld from the nav for a
    normal player: `_nav_html` never emits the markup for them unless the viewer
    is staff, so it is not merely CSS-hidden. The route itself still 403s — an
    unlinked tab is not the gate.
    """
    body = icon or _ICONS.get(key, "")
    for s in _SECTIONS:
        if s["key"] == key:
            s.update({"label": label, "path": path, "icon": body, "order": order,
                      "staff_only": bool(staff_only)})
            return
    _SECTIONS.append({"key": key, "label": label, "path": path, "icon": body,
                      "order": order, "staff_only": bool(staff_only)})
    _SECTIONS.sort(key=lambda s: (s["order"], s["label"]))


def sections() -> list[dict]:
    return list(_SECTIONS)


# ══════════════════════════════════════════════════════════════════════════
# Money providers. Core (available/held) is always present. Savings and debt
# belong to Osentar Bank, which owns no table in this schema yet — so until
# Osentar registers, those segments do not render. An absent figure is honest;
# a placeholder is not.
# ══════════════════════════════════════════════════════════════════════════

_MONEY_PROVIDERS: dict[str, Callable[[str], Optional[dict]]] = {}


def register_money_provider(name: str, fn: Callable[[str], Optional[dict]]) -> None:
    """Register a strip contributor.

    `fn(user_id)` returns a dict or None. Recognised keys:
        savings      int  — coins held by the provider on the user's behalf
        savings_note str  — one short line, e.g. "3.2% APR"
        debt         int  — outstanding principal owed by the user

    Net position is `available + held + savings - debt` and renders ONLY when
    every term it names is available. A net figure with a silently-missing term
    is a wrong number, and this strip is on every page.
    """
    _MONEY_PROVIDERS[name] = fn


SERVICE_NAMES = {
    "core": "V Tech core",
    "osentar": "Osentar Bank",
    "estates": "Estates",
    "markets": "Markets",
    "lands": "Lands & Auctions",
    "messages": "Messages",
    "hub": "V Tech Hub",
}


def _service_label(service: str) -> str:
    """Real names over internal ids, everywhere a user looks."""
    s = str(service or "")
    return SERVICE_NAMES.get(s, s.replace("treasury:", "").replace("_", " ").title() or "Unknown")


# ══════════════════════════════════════════════════════════════════════════
# Hub-owned SQLite: idempotency keys + the attack log.
# Own connection (autocommit + explicit BEGIN IMMEDIATE), same file as the
# ledger, for the same reason ledger_v2 keeps its own: changing isolation_level
# on Restocker_db's shared thread-local connection would alter transaction
# behaviour for every other caller on that thread.
# ══════════════════════════════════════════════════════════════════════════

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_idempotency (
    key         TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    state       TEXT NOT NULL,        -- minted | claimed | done
    minted_at   REAL NOT NULL,
    claimed_at  REAL,
    done_at     REAL,
    status      INTEGER,
    response    TEXT
);
CREATE INDEX IF NOT EXISTS ix_hub_idem_user ON hub_idempotency(user_id, state);

CREATE TABLE IF NOT EXISTS hub_attack_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    session_user TEXT,
    endpoint  TEXT,
    detail    TEXT,
    ip        TEXT
);
"""

_conn_cache: dict[tuple, sqlite3.Connection] = {}


def _hub_db_path() -> str:
    override = os.getenv("HUB_DB_PATH", "").strip()
    if override:
        return override
    return str(_db().DB_PATH)


def _hub_conn() -> sqlite3.Connection:
    import threading
    path = _hub_db_path()
    key = (threading.get_ident(), path)
    cached = _conn_cache.get(key)
    if cached is not None:
        return cached
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_SCHEMA)
    _conn_cache[key] = conn
    return conn


def reset_connections() -> None:
    """Drop cached connections — for tests that swap HUB_DB_PATH."""
    for c in list(_conn_cache.values()):
        try:
            c.close()
        except Exception:
            pass
    _conn_cache.clear()


def _record_attack(kind: str, request: Any, session_user: Optional[str],
                   endpoint: str, detail: str) -> None:
    """Log a request that tried to assert an identity it wasn't given.

    Deliberately never raises: a failure to WRITE the alarm must not become a
    failure to REFUSE the request.
    """
    ip = ""
    try:
        ip = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    except Exception:
        pass
    log.warning("[hub attack] %s endpoint=%s session_user=%s ip=%s detail=%s",
                kind, endpoint, session_user, ip, detail)
    try:
        _hub_conn().execute(
            "INSERT INTO hub_attack_log (ts, kind, session_user, endpoint, detail, ip) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), kind, session_user, endpoint, detail[:500], ip),
        )
    except Exception as e:  # pragma: no cover
        log.warning("[hub attack] alarm write failed: %s", e)


_IDENTITY_FIELDS = ("user_id", "uid", "userid", "discord_id", "from_user",
                    "to_user", "owner_id", "actor", "as_user", "on_behalf_of")


def _scan_body_identity(body: dict, request: Any, uid: str, endpoint: str) -> None:
    """A user id in a request body is IGNORED and logged as an attack signal.

    This is not paranoia about a stray field: the whole site is transactional,
    so the difference between "identity is the session" and "identity is
    whatever the body says" is the difference between a wallet and a faucet.
    Nothing downstream reads these fields; this only raises the alarm.
    """
    if not isinstance(body, dict):
        return
    found = {k: body[k] for k in _IDENTITY_FIELDS if k in body}
    if found:
        _record_attack("body_supplied_identity", request, uid, endpoint,
                       json.dumps(found, default=str))


# ══════════════════════════════════════════════════════════════════════════
# Idempotency — claim-first, single-use, replay-safe.
# ══════════════════════════════════════════════════════════════════════════

def mint_key(user_id: str, endpoint: str) -> str:
    """Mint a single-use idempotency key at PAGE RENDER, bound to this user and
    this endpoint. The browser never invents one: a caller-chosen key would let
    an attacker pre-burn a victim's key, and a key not bound to a user would let
    one session's key be spent by another."""
    key = secrets.token_urlsafe(24)
    _hub_conn().execute(
        "INSERT INTO hub_idempotency (key, user_id, endpoint, state, minted_at) "
        "VALUES (?,?,?, 'minted', ?)",
        (key, str(user_id), str(endpoint), time.time()),
    )
    return key


class KeyVerdict:
    """Result of trying to spend a key.

    status is one of:
        ok            — claimed, caller may act exactly once
        unknown       — never minted, or expired: refuse
        forbidden     — minted for a different USER: refuse + alarm
        wrong_subject — this user's key, minted on a different endpoint/ticker:
                        refuse and say which, no alarm — it is his own key
        in_progress   — claimed but not finished: refuse, do not act twice
        replay        — already completed: return `body`/`http_status` verbatim
    """

    __slots__ = ("status", "body", "http_status")

    def __init__(self, status: str, body: Optional[dict] = None, http_status: int = 200):
        self.status = status
        self.body = body
        self.http_status = http_status


def claim_key(key: str, user_id: str, endpoint: str) -> KeyVerdict:
    """Claim-first: one atomic UPDATE ... WHERE still-minted. Act only if we won.

    Acting first and marking afterwards double-spends on any crash between the
    two — the exact failure this economy has already paid for on the payout path.
    """
    key = str(key or "").strip()
    uid = str(user_id)
    if not key:
        return KeyVerdict("unknown")
    conn = _hub_conn()
    now = time.time()
    cur = conn.execute(
        "UPDATE hub_idempotency SET state='claimed', claimed_at=? "
        "WHERE key=? AND user_id=? AND endpoint=? AND state='minted' AND minted_at > ?",
        (now, key, uid, str(endpoint), now - IDEM_TTL),
    )
    if cur.rowcount == 1:
        return KeyVerdict("ok")

    row = conn.execute("SELECT * FROM hub_idempotency WHERE key=?", (key,)).fetchone()
    if row is None:
        return KeyVerdict("unknown")
    if row["user_id"] != uid:
        return KeyVerdict("forbidden")
    if row["endpoint"] != str(endpoint):
        # His own key, minted somewhere else. Since the endpoint now carries the ticker
        # (`markets/trade:VTEC`), this is the confirm-screen-vs-submitted-trade mismatch
        # of WEB_ATTACK finding 7 — a wiring bug or a hand-rolled POST, not key theft.
        # Refusing it as theft would put an attack row and a scary sentence in front of
        # a player who did nothing wrong, so it gets its own verdict.
        return KeyVerdict("wrong_subject", {"minted_for": str(row["endpoint"])})
    if row["state"] == "done":
        try:
            body = json.loads(row["response"] or "{}")
        except Exception:
            body = {"ok": False, "error": "original result unreadable"}
        return KeyVerdict("replay", body, int(row["status"] or 200))
    if row["state"] == "claimed":
        return KeyVerdict("in_progress")
    return KeyVerdict("unknown")     # minted but expired


def complete_key(key: str, body: dict, status: int = 200) -> None:
    """Record the outcome so a replay returns THIS result, not a second action."""
    _hub_conn().execute(
        "UPDATE hub_idempotency SET state='done', done_at=?, status=?, response=? "
        "WHERE key=? AND state='claimed'",
        (time.time(), int(status), json.dumps(body, default=str), str(key)),
    )


def release_key(key: str) -> None:
    """Return a key to 'minted' — ONLY for a rejection that provably moved
    nothing (a validation failure before the engine was called). Never after the
    engine ran: an engine failure is a real outcome and must be replayable as
    that outcome, not silently retried into a second attempt."""
    _hub_conn().execute(
        "UPDATE hub_idempotency SET state='minted', claimed_at=NULL WHERE key=? AND state='claimed'",
        (str(key),),
    )


def sweep_keys(now: Optional[float] = None) -> int:
    """Delete keys older than the TTL. Per-row state is the progress marker, so a
    half-finished sweep loses nothing."""
    cur = _hub_conn().execute(
        "DELETE FROM hub_idempotency WHERE minted_at < ? AND state <> 'claimed'",
        ((now or time.time()) - IDEM_TTL,),
    )
    return cur.rowcount or 0


# ══════════════════════════════════════════════════════════════════════════
# IDENTITY — the only way a handler learns who is asking.
# ══════════════════════════════════════════════════════════════════════════

def current_user(request: Any) -> Optional[dict]:
    """`{user_id, name, csrf}` for the request's session cookie, or None.

    Delegates to `Restocker_web._session_user`, ON PURPOSE. There is exactly one
    session store (`_SESSIONS` + data/state/web_sessions.yml) and exactly one
    cookie (`vtm_sess`); a second store would mean a user "logged in" on one half
    of the site and anonymous on the other, and two places to get expiry wrong.

    Discord OAuth below is an additional way IN to that same store, not a second
    identity system.

    Resolved through `vt_web_shell.session_user`, which is the site's single whoami:
    in production it delegates to the very same `Restocker_web._session_user` this
    used to call directly, so the deployed behaviour is unchanged — but it also means
    the hub reads the SAME identity the money chokepoints read, including the test
    seam, so there is one identity path and not two that can disagree about who the
    user is.
    """
    try:
        import vt_web_shell as _shell
        sess = _shell.session_user(request)
    except Exception as e:      # never fail open
        log.warning("[hub] session lookup failed: %s", e)
        return None
    if not sess:
        return None
    uid = str(sess.get("user_id") or "").strip()
    if not uid:
        return None
    return {"user_id": uid, "name": sess.get("name") or "", "csrf": sess.get("csrf") or ""}


def _mint_session(user_id: str, name: str) -> str:
    """Write a session into Restocker_web's store and return the bearer token.

    Same record shape `_handle_api_link` writes, so `_session_user` reads it
    without knowing which door the user came through.
    """
    w = _web()
    token = secrets.token_urlsafe(24)
    sess = {
        "user_id": str(user_id),
        "name": str(name or ""),
        "csrf": secrets.token_urlsafe(24),
        "expires": time.time() + SESSION_TTL,
    }
    w._SESSIONS[token] = sess
    try:
        stored = w._load_sessions()
        stored[token] = sess
        w._save_sessions(stored)
    except Exception as e:
        # In-memory login still works; it just won't survive a bot restart.
        log.warning("[hub] session persist failed: %s", e)
    return token


def _set_session_cookie(resp: Any, token: str) -> None:
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, secure=not _INSECURE_COOKIES,
                    max_age=SESSION_TTL, samesite="Lax", path="/")


def _csrf_ok(request: Any, user: dict, supplied: str = "") -> bool:
    """Defence in depth on top of SameSite=Lax. Accepts the token from the form
    body (plain HTML POSTs) or the header (fetch callers)."""
    want = user.get("csrf") or ""
    got = supplied or request.headers.get("X-CSRF-Token", "") or ""
    if not want:
        return False
    return secrets.compare_digest(str(want), str(got))


def oauth_enabled() -> bool:
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI)


# ══════════════════════════════════════════════════════════════════════════
# MONEY — computed server-side, from the ledger, for every page.
# ══════════════════════════════════════════════════════════════════════════

def _open_holds(user_id: str) -> list[dict]:
    """Every OPEN hold against this user's wallet, whoever placed it.

    `ledger_v2.list_holds` is deliberately per-service — a service may only see
    and touch its own holds. This read crosses that boundary on purpose and only
    here: it is read-only, scoped to the caller's OWN wallet, and it exists
    because the drawer's entire job is to answer "what is holding MY coins".
    Nothing in this module mutates a hold.
    """
    try:
        rows = _ledger()._conn().execute(
            "SELECT hold_id, service, amount, captured_amount, released_amount, "
            "       reason, created_at, expires_at "
            "FROM ledger_holds WHERE user_id=? AND state='open' "
            "ORDER BY amount DESC LIMIT 100",
            (str(user_id),),
        ).fetchall()
    except Exception as e:
        log.warning("[hub] hold read failed: %s", e)
        return []
    out = []
    for r in rows:
        out.append({
            "hold_id": r["hold_id"],
            "service": r["service"],
            "service_label": _service_label(r["service"]),
            "amount": int(r["amount"] or 0),
            "reason": r["reason"] or "",
            "expires_at": r["expires_at"],
        })
    return out


def money_snapshot(user_id: str) -> dict:
    """The strip's figures. Ledger is the source; nothing here is cached, because
    a stale available balance next to a Confirm button is a wrong number.

    available = balance - held, per LEDGER_API_v2 §4. `held` is never stored.
    `savings`/`debt`/`net` are None when no provider supplies them.
    """
    snap: dict[str, Any] = {
        "user_id": str(user_id),
        "balance": None, "held": None, "available": None,
        "frozen": False, "frozen_reason": None,
        "holds": [], "savings": None, "savings_note": "", "debt": None,
        "net": None, "ledger_ok": False,
    }
    try:
        bal = _ledger().get_balance(str(user_id))
        snap.update({
            "balance": int(bal.get("balance") or 0),
            "held": int(bal.get("held") or 0),
            "available": int(bal.get("available") or 0),
            "frozen": bool(bal.get("frozen")),
            "frozen_reason": bal.get("frozen_reason"),
            "ledger_ok": True,
        })
    except Exception as e:
        log.warning("[hub] ledger balance failed for %s: %s", user_id, e)
        return snap

    snap["holds"] = _open_holds(user_id)

    for name, fn in _MONEY_PROVIDERS.items():
        try:
            part = fn(str(user_id)) or {}
        except Exception as e:
            log.warning("[hub] money provider %s failed: %s", name, e)
            continue
        if part.get("savings") is not None:
            snap["savings"] = int(snap["savings"] or 0) + int(part["savings"])
            if part.get("savings_note"):
                snap["savings_note"] = str(part["savings_note"])
        if part.get("debt") is not None:
            snap["debt"] = int(snap["debt"] or 0) + int(part["debt"])

    if snap["savings"] is not None and snap["debt"] is not None:
        snap["net"] = snap["available"] + snap["held"] + snap["savings"] - snap["debt"]
    return snap


# ══════════════════════════════════════════════════════════════════════════
# Rendering helpers
# ══════════════════════════════════════════════════════════════════════════

def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def n(v: Any) -> str:
    """Integer coins with thousands separators. Coins are integers; a fractional
    coin is a bug, so this floors rather than printing 12,499.999999."""
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return "—"


def cn(v: Any) -> str:
    """A figure with its unit. A number with no unit is a bug."""
    return f'{n(v)}<span class="coin">c</span>'


def px(v: Any) -> str:
    """A share price — two decimals, because the exchange quotes them that way."""
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _svg(body: str, cls: str = "i") -> str:
    c = f' class="{cls}"' if cls else ""
    return (f'<svg{c} viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def human_date(iso: Any) -> str:
    """`2026-09-01T00:00:00Z` -> `1 Sep 2026`. Dates a person reads, not a
    timestamp a machine wrote."""
    s = str(iso or "").strip()
    if len(s) < 10:
        return s
    try:
        y, m, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
        return f"{d} {_MONTHS[m - 1]} {y}"
    except (ValueError, IndexError):
        return s


def _initials(name: str, uid: str) -> str:
    nm = (name or "").strip()
    if nm:
        parts = [p for p in nm.replace("_", " ").split() if p]
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return nm[:2].upper()
    return uid[-2:] if uid else "??"


# The shared CSS block, served once per page from the mockups verbatim.
# Zero border-radius (status dots excepted), IBM Plex Mono figures with
# tabular-nums, Space Grotesk UI, #080808 dot grid, one accent #22FF7A.
THEME_CSS = r"""
:root{
  --font-data:'IBM Plex Mono',ui-monospace,Menlo,monospace;
  --font-ui:'Space Grotesk',system-ui,sans-serif;
  --bg:#080808;--surface:#0f0f0f;--panel2:#151515;--border:#1E1E1E;--border-strong:#2A2A2A;
  --text:#F4F4F4;--text-body:#B4B4B4;--muted:#6a6a6a;--faint:#3f3f3f;
  --green:#22FF7A;--green-dim:#0f7a3a;--accent:#22FF7A;--red:#FF4D4D;--amber:#F5A623;
  --blue:#4A9EFF;--purple:#B47FFF;--nether:#FF6B35;
  --money-available:var(--text);--money-held:var(--amber);--money-debt:var(--red);
  --money-save:var(--green);--headh:54px;--navh:46px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark}
body{background:var(--bg);background-image:radial-gradient(#151515 .5px,transparent .5px);background-size:24px 24px;color:var(--text);font-family:var(--font-ui);font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased;min-height:100vh}
.mono,td,.num,input,.pill,.tag,.val,.fv,.tick,.hold-amt,.kfig,.big{font-family:var(--font-data);font-variant-numeric:tabular-nums slashed-zero}
a{color:inherit;text-decoration:none}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
::selection{background:rgba(34,255,122,.22)}
svg.i{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;flex:0 0 auto}

header{position:sticky;top:0;z-index:90;height:var(--headh);background:rgba(15,15,15,.9);backdrop-filter:blur(10px);border-bottom:1px solid var(--border);padding:0 24px;display:flex;align-items:center;justify-content:space-between}
.logo{display:flex;align-items:center;gap:12px;cursor:pointer}
.logo-icon{width:30px;height:30px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:#000;font-weight:700;font-size:16px;box-shadow:0 0 24px rgba(34,255,122,.4)}
.logo-text{font-size:14px;font-weight:600}
.logo-sub{font-family:var(--font-data);font-size:8.5px;color:var(--muted);margin-top:2px;text-transform:uppercase;letter-spacing:.18em}
.header-right{display:flex;align-items:center;gap:12px}
.user-tag{display:flex;align-items:center;gap:9px;padding-left:12px;border-left:1px solid var(--border)}
.user-mark{width:22px;height:22px;border:1px solid var(--border-strong);background:var(--panel2);display:flex;align-items:center;justify-content:center;font-family:var(--font-data);font-size:9.5px;color:var(--text-body)}
.auth-name{font-family:var(--font-data);color:var(--accent);font-weight:500;font-size:12px}
.svc-note{font-family:var(--font-data);font-size:9.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.14em}

nav{position:sticky;top:var(--headh);z-index:80;display:flex;gap:2px;background:var(--surface);border-bottom:1px solid var(--border);padding:0 16px;overflow-x:auto;height:var(--navh);scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
.nav-tab{display:flex;align-items:center;gap:7px;height:var(--navh);padding:0 16px;font-size:12px;font-weight:500;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;user-select:none;background:none}
.nav-tab svg{opacity:.7}
.nav-tab:hover{color:var(--text)}
.nav-tab[aria-current="true"]{color:var(--accent);border-bottom-color:var(--accent)}
.nav-tab[aria-current="true"] svg{opacity:1}
.nav-badge{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:16px;margin-left:6px;padding:0 4px;background:var(--accent);color:#000;font-family:var(--font-data);font-size:10px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1}

.strip-wrap{position:sticky;top:calc(var(--headh) + var(--navh));z-index:70;background:var(--surface);border-bottom:1px solid var(--border)}
.strip{max-width:1180px;margin:0 auto;padding:0 24px;display:flex;align-items:stretch;min-height:60px}
.seg{display:flex;flex-direction:column;justify-content:center;padding:10px 22px;position:relative;flex:0 0 auto;text-align:left}
.seg + .seg::before{content:"";position:absolute;left:0;top:12px;bottom:12px;width:1px;background:var(--border)}
.seg:first-child{padding-left:0}
.seg .lab{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600;margin-bottom:3px;display:flex;align-items:center;gap:6px}
.seg .val{font-size:19px;font-weight:600;letter-spacing:-.01em;line-height:1.15}
.seg .sub{font-size:10.5px;color:var(--muted)}
.v-avail{color:var(--money-available)}
.v-held{color:var(--money-held)}
.v-save{color:var(--money-save)}
.v-net{color:var(--text)}
.coin{font-family:var(--font-data);font-size:.62em;font-weight:500;color:var(--muted);margin-left:3px;letter-spacing:0}
.seg.held-seg{cursor:pointer}
.seg.held-seg:hover{background:var(--panel2)}
.seg.held-seg .lab{color:var(--money-held)}
.chev{display:inline-flex;transition:transform .18s ease;color:var(--money-held)}
.chev svg{width:12px;height:12px}
.strip-wrap.open .chev{transform:rotate(180deg)}
.strip .spacer{flex:1 1 auto}
.seg.net-seg{padding-right:0;text-align:right;align-items:flex-end}
.net-note{font-size:10.5px;color:var(--muted)}
.strip-note{max-width:1180px;margin:0 auto;padding:0 24px 9px;font-size:10.5px;color:var(--muted)}

.drawer{max-height:0;overflow:hidden;transition:max-height .22s ease;border-top:1px solid transparent}
.strip-wrap.open .drawer{max-height:400px;border-top-color:var(--border);overflow-y:auto}
.drawer-inner{max-width:1180px;margin:0 auto;padding:12px 24px 16px}
.drawer-head{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600;margin-bottom:9px;display:flex;align-items:center;gap:10px}
.drawer-head::after{content:"";flex:1;height:1px;background:var(--border)}
.hold-row{display:flex;align-items:center;gap:12px;width:100%;text-align:left;padding:9px 12px;border:1px solid var(--border);background:var(--panel2);margin-bottom:6px;transition:border-color .12s}
.hold-row:hover{border-color:var(--border-strong)}
.hold-dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;display:inline-block;background:var(--money-held)}
.hold-main{flex:1;min-width:0}
.hold-t{font-size:12.5px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.hold-s{font-size:10.5px;color:var(--muted);display:block}
.hold-amt{font-size:14px;font-weight:600;color:var(--money-held);white-space:nowrap}
.drawer-foot{display:flex;justify-content:space-between;align-items:center;gap:16px;padding-top:9px;margin-top:6px;border-top:1px solid var(--border);font-size:11px;color:var(--muted)}
.drawer-foot b{color:var(--money-held);font-size:13px;font-family:var(--font-data);font-variant-numeric:tabular-nums slashed-zero}

main{max-width:1180px;margin:0 auto;padding:24px 24px 80px;animation:f .25s ease}
@keyframes f{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.page-head{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:21px;font-weight:600;letter-spacing:-.01em;margin:0}
.page-sub{color:var(--muted);font-size:12.5px;margin-top:3px}
.section-h{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:24px 0 12px;display:flex;align-items:center;gap:10px;font-weight:600}
.section-h::after{content:"";flex:1;height:1px;background:var(--border)}
.tile-h{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;gap:10px;font-weight:600}

.bento{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--border);padding:18px 20px;position:relative}
.s12{grid-column:span 12}.s7{grid-column:span 7}.s6{grid-column:span 6}
.s5{grid-column:span 5}.s4{grid-column:span 4}.s3{grid-column:span 3}
@media(max-width:900px){.s7,.s6,.s5,.s4,.s3{grid-column:span 12}}

.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 8px;font-size:11px;border:1px solid var(--border);color:var(--text-body);white-space:nowrap}
.pill.good{border-color:rgba(34,255,122,.3);color:var(--green)}
.pill.warn{border-color:rgba(245,166,35,.3);color:var(--amber)}
.pill.crit{border-color:rgba(255,77,77,.3);color:var(--red)}
.pill.mine{border-color:rgba(74,158,255,.35);color:var(--blue)}
.up{color:var(--green)}.down{color:var(--red)}
.muted{color:var(--muted)}.sec{color:var(--text-body)}.amb{color:var(--amber)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;background:var(--accent);color:#000;border:none;padding:8px 14px;font-family:var(--font-ui);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;cursor:pointer}
.btn:hover{background:#4dff96}
.btn.ghost{background:transparent;border:1px solid var(--border);color:var(--text-body)}
.btn.ghost:hover{background:var(--panel2);border-color:var(--border-strong);color:var(--text)}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn.sm{padding:6px 10px;font-size:10px}
.row{display:flex;align-items:center;gap:10px}
.between{display:flex;align-items:center;justify-content:space-between;gap:12px}
.wrapf{flex-wrap:wrap}

table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border)}
th,td{padding:9px 13px;text-align:right;border-bottom:1px solid var(--border);font-size:12.5px}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600;font-family:var(--font-ui);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
tbody tr:hover{background:var(--panel2)}
tr:last-child td{border-bottom:none}
.tick{font-weight:600;letter-spacing:.02em}
.tname{font-size:10.5px;color:var(--muted);font-family:var(--font-ui)}
.tablewrap{overflow-x:auto}

.bar{height:6px;background:var(--panel2);overflow:hidden;position:relative}
.bar > i{display:block;height:100%;background:linear-gradient(90deg,var(--green),#17b558)}

.chart-legend{display:flex;gap:16px;font-size:10.5px;color:var(--muted);align-items:center;text-transform:uppercase;letter-spacing:.08em}
.lg{display:flex;align-items:center;gap:6px}
.lg i{width:12px;height:2px;display:block}

.amount-wrap{position:relative;margin-bottom:10px;max-width:300px}
.amount{width:100%;padding:13px 14px 13px 40px;font-size:22px;font-weight:600;background:var(--bg);border:1px solid var(--border);color:var(--text);outline:none;letter-spacing:-.01em}
.amount:focus{border-color:var(--accent)}
.amount-wrap .cn{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:15px;font-family:var(--font-data)}
.kv{display:flex;justify-content:space-between;gap:16px;padding:8px 0;font-size:12.5px;border-bottom:1px solid var(--border)}
.kv:last-of-type{border-bottom:none}
.kv .k{color:var(--muted)}
.kv .v{font-weight:600;white-space:nowrap;text-align:right;font-family:var(--font-data);font-variant-numeric:tabular-nums slashed-zero}
.kv.total{border-top:1px solid var(--border);border-bottom:none;margin-top:6px;padding-top:12px;font-size:13.5px}
.notebox{margin-top:14px;padding:10px 12px;font-size:11px;line-height:1.55;background:rgba(245,166,35,.06);border:1px solid rgba(245,166,35,.25);color:#d9ae6a}
.notebox.blue{background:rgba(74,158,255,.06);border-color:rgba(74,158,255,.25);color:#9dc3f0}
.okbox{margin-top:14px;padding:11px 12px;font-size:11.5px;line-height:1.55;background:rgba(34,255,122,.06);border:1px solid rgba(34,255,122,.25);color:#8fe0b2}
.errbox{margin-top:14px;padding:11px 12px;font-size:11.5px;line-height:1.55;background:rgba(255,77,77,.06);border:1px solid rgba(255,77,77,.28);color:#f0a0a0}
.side-pick{display:flex;gap:0;border:1px solid var(--border);width:max-content;margin-bottom:14px}
.side-pick label{padding:7px 18px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);cursor:pointer;border-right:1px solid var(--border)}
.side-pick label:last-child{border-right:none}
.side-pick input{position:absolute;opacity:0;pointer-events:none}
.side-pick input:checked + span{color:#000;background:var(--accent);display:block;margin:-7px -18px;padding:7px 18px}
.empty{padding:16px 0;color:var(--muted);font-size:12px}
.foot{color:var(--faint);font-size:11px;margin-top:12px;line-height:1.5}
.login-card{max-width:420px;margin:80px auto;background:var(--surface);border:1px solid var(--border);padding:26px 26px 22px}
.login-card h1{margin-bottom:6px}
.login-card p{color:var(--text-body);font-size:12.5px;margin-bottom:16px}
.login-card code{font-family:var(--font-data);color:var(--accent)}

@media (max-width:760px){
  header{padding:0 14px}
  nav{padding:0 6px}
  .nav-tab{padding:0 11px}
  .svc-note{display:none}
  main{padding:20px 14px 60px}
  .strip{padding:0 14px;flex-wrap:wrap;min-height:0}
  .seg{padding:9px 14px;flex:1 1 auto;min-width:44%}
  .seg:first-child{padding-left:0}
  .seg + .seg::before{display:none}
  .seg.net-seg{align-items:flex-start;text-align:left;padding-right:14px}
  .strip .spacer{display:none}
  .seg .val{font-size:17px}
  .drawer-inner{padding:10px 14px 14px}
  .hide-sm{display:none}
  th.hide-sm,td.hide-sm{display:none}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms !important;animation-iteration-count:1 !important;transition-duration:.001ms !important;scroll-behavior:auto !important}
}
"""

# The only script in the shell: toggling the held drawer. Every figure on the
# page was computed server-side, so nothing here fetches or formats money.
_STRIP_JS = """
(function(){
  var w=document.getElementById('stripWrap'),b=document.getElementById('heldSeg');
  if(!w||!b)return;
  b.addEventListener('click',function(){
    var open=w.classList.toggle('open');
    b.setAttribute('aria-expanded',open?'true':'false');
  });
})();
/* Unread messages in the nav. Same badge, same endpoint and same fail-silent rule as
   `vt_web_shell`'s copy — the two shells render different navs, so each fills its own
   badge, and neither breaks when the messages section is not mounted. */
(function(){
  var el=document.getElementById('navUnread');
  if(!el)return;
  function go(){
    fetch('/api/messages/unread',{credentials:'same-origin'})
      .then(function(r){return r.json();})
      .then(function(j){
        if(j&&j.ok&&Number(j.unread)>0){
          el.textContent=Number(j.unread).toLocaleString('en-US');
          el.style.display='inline-flex';
        }else{el.style.display='none';}
      })
      .catch(function(){el.style.display='none';});
  }
  go(); setInterval(go,60000);
})();
"""


def money_strip_html(snap: dict) -> str:
    """The persistent strip. Rendered from `money_snapshot`, server-side, on
    every page — including the drawer that NAMES what is holding the coins.

    Segments whose source does not exist are omitted, not zeroed.
    """
    if not snap.get("ledger_ok"):
        return ('<div class="strip-wrap"><div class="strip-note">'
                'Wallet unavailable — the ledger did not answer. No figures are shown '
                'rather than stale ones.</div></div>')

    holds = snap.get("holds") or []
    held = int(snap.get("held") or 0)
    avail = int(snap.get("available") or 0)

    segs = [
        '<div class="seg"><div class="lab">Wallet · available</div>'
        f'<div class="val v-avail num">{cn(avail)}</div>'
        '<div class="sub">spendable right now</div></div>'
    ]

    held_sub = (f"reserved by {len(holds)} thing{'s' if len(holds) != 1 else ''}"
                if holds else "nothing reserved")
    chev = _svg(_CHEVRON, "")
    segs.append(
        '<button class="seg held-seg" id="heldSeg" aria-expanded="false" aria-controls="heldDrawer">'
        f'<div class="lab">Held <span class="chev">{chev}</span></div>'
        f'<div class="val v-held num">{cn(held)}</div>'
        f'<div class="sub">{esc(held_sub)}</div></button>'
    )

    # Savings and net render only when every term they name has a real source.
    if snap.get("savings") is not None:
        note = snap.get("savings_note") or ""
        segs.append('<div class="seg"><div class="lab">Savings · Osentar</div>'
                    f'<div class="val v-save num">{cn(snap["savings"])}</div>'
                    f'<div class="sub">{esc(note)}</div></div>')

    net_html = ""
    if snap.get("net") is not None:
        net_html = ('<div class="spacer"></div>'
                    '<div class="seg net-seg"><div class="lab">Net position</div>'
                    f'<div class="val v-net num">{cn(snap["net"])}</div>'
                    '<div class="net-note">available + held + savings &minus; loan · '
                    'bonds &amp; land excluded</div></div>')

    rows = []
    for h in holds:
        exp = (f" · expires {esc(human_date(h['expires_at']))}"
               if h.get("expires_at") else " · no expiry set")
        reason = h.get("reason") or "no reason recorded"
        rows.append(
            '<div class="hold-row"><span class="hold-dot"></span><span class="hold-main">'
            f'<span class="hold-t">{esc(reason)}</span>'
            f'<span class="hold-s">{esc(h["service_label"])}{exp}</span></span>'
            f'<span class="hold-amt num">{cn(h["amount"])}</span></div>'
        )
    if not rows:
        # Empty states are EMPTY. One muted line, no illustration, no advice.
        rows.append('<div class="empty">Nothing is holding your coins.</div>')

    drawer = (
        '<div class="drawer" id="heldDrawer"><div class="drawer-inner">'
        '<div class="drawer-head">What is holding your coins</div>'
        + "".join(rows) +
        '<div class="drawer-foot">'
        '<span>Holds are escrow — the coins are yours until captured, and every hold has an expiry.</span>'
        f'<span>Total held <b>{n(held)}</b></span></div></div></div>'
    )

    note = ""
    if snap.get("savings") is None or snap.get("debt") is None:
        note = ('<div class="strip-note">Savings and net position need Osentar Bank — '
                'not connected.</div>')

    frozen = ""
    if snap.get("frozen"):
        frozen = ('<div class="strip-note" style="color:var(--red)">Wallet frozen — '
                  f'{esc(snap.get("frozen_reason") or "no reason recorded")}</div>')

    return ('<div class="strip-wrap" id="stripWrap"><div class="strip">'
            + "".join(segs) + net_html + '</div>' + frozen + note + drawer + '</div>')


def _is_staff_user(user: Optional[dict]) -> bool:
    if not user:
        return False
    try:
        import vt_web_shell as _shell
        return _shell.is_staff(user)
    except Exception:  # pragma: no cover
        return False


def _nav_html(active: str, user: Optional[dict] = None) -> str:
    staff = _is_staff_user(user)
    tabs = []
    for s in _SECTIONS:
        if s.get("staff_only") and not staff:
            continue
        cur = ' aria-current="true"' if s["key"] == active else ""
        icon = _svg(s["icon"]) if s["icon"] else ""
        badge = ('<span class="nav-badge" id="navUnread" style="display:none"></span>'
                 if s["key"] == "messages" else "")
        tabs.append(f'<a class="nav-tab" href="{esc(s["path"])}"{cur}>{icon}'
                    f'{esc(s["label"])}{badge}</a>')
    return "<nav>" + "".join(tabs) + "</nav>"


def page(title: str, active: str, user: Optional[dict], snap: Optional[dict],
         body: str, subtitle: str = "") -> str:
    """The shell. Every section renders through this so the nav, theme, strip and
    drawer exist exactly once."""
    if user:
        who = (f'<span class="user-tag"><span class="user-mark">'
               f'{esc(_initials(user.get("name", ""), user["user_id"]))}</span>'
               f'<span class="auth-name">{esc(user.get("name") or user["user_id"])}</span></span>')
    else:
        who = f'<a class="btn ghost sm" href="{HUB_PREFIX}/login">Sign in</a>'

    strip = money_strip_html(snap) if (user and snap) else ""
    svc = " · ".join(s["label"] for s in _SECTIONS) or "V Tech"
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>{THEME_CSS}</style>
</head><body>
<header>
  <a class="logo" href="{HUB_PREFIX}">
    <div class="logo-icon">V</div>
    <div><div class="logo-text">V Tech Hub</div><div class="logo-sub">One economy</div></div>
  </a>
  <div class="header-right"><span class="svc-note">{esc(svc)}</span>{who}</div>
</header>
{_nav_html(active, user)}
{strip}
<main>{body}</main>
<script>{_STRIP_JS}</script>
</body></html>"""


def _html(body: str, status: int = 200) -> Any:
    return web.Response(text=body, content_type="text/html", status=status)


def _login_required_page(request: Any) -> Any:
    """A logged-out request to any hub page gets 401 and the sign-in card. No
    section content, no figures, no partial render."""
    ways = []
    if oauth_enabled():
        ways.append(f'<a class="btn" href="{HUB_PREFIX}/auth/discord">Sign in with Discord</a>')
    ways.append(f'<a class="btn ghost" href="{HUB_PREFIX}/login">Use a login code</a>')
    body = ('<div class="login-card"><h1>Sign in</h1>'
            '<p>The hub is transactional — every page is your own money, so nothing '
            'renders until we know who you are.</p>'
            f'<div class="row wrapf">{"".join(ways)}</div></div>')
    return _html(page("V Tech Hub — sign in", "", None, None, body), status=401)


def _json(body: dict, status: int = 200) -> Any:
    return web.json_response(body, status=status)


# ══════════════════════════════════════════════════════════════════════════
# The money-POST wrapper. Every transactional handler in every section goes
# through this: session identity, CSRF, body-identity alarm, claim-first key.
# ══════════════════════════════════════════════════════════════════════════

async def idempotent_post(request: Any, endpoint: str,
                          work: Callable[[dict, dict], Any]) -> Any:
    """Run `work(user, fields)` at most once per minted key.

    `work` returns `(body_dict, http_status)` and MUST be safe to not-run: it is
    called only if this request won the claim.

    Returns an aiohttp Response. A replay returns the ORIGINAL body and status,
    byte for byte, so a user hammering Confirm buys shares once.
    """
    user = current_user(request)
    if not user:
        return _json({"ok": False, "error": "Log in first."}, 401)
    uid = user["user_id"]

    # View-as is read-only. This wrapper is the second of the two chokepoints every
    # mutating route passes through (the first is `vt_web_shell.require_post_session`);
    # a staff member viewing the site as somebody cannot trade here, and the refused
    # attempt is audited. Placed before CSRF and the claim so it fires on the bare
    # request, keyed off the real session — never a body-supplied id.
    try:
        import vt_web_shell as _shell
        _imp = _shell.refuse_if_impersonating(request)
        if _imp is not None:
            return _imp
    except Exception:  # pragma: no cover - shell always present in the mounted app
        pass

    fields = await _read_fields(request)
    _scan_body_identity(fields, request, uid, endpoint)

    if not _csrf_ok(request, user, str(fields.get("csrf") or "")):
        return _json({"ok": False, "error": "Bad or missing CSRF token."}, 403)

    key = str(fields.get("idempotency_key") or "").strip()
    verdict = claim_key(key, uid, endpoint)
    if verdict.status == "replay":
        body = dict(verdict.body or {})
        body["replayed"] = True
        return _json(body, verdict.http_status)
    if verdict.status == "forbidden":
        _record_attack("idempotency_key_theft", request, uid, endpoint, f"key={key[:8]}…")
        return _json({"ok": False, "error": "That form does not belong to this session."}, 403)
    if verdict.status == "wrong_subject":
        minted_for = str((verdict.body or {}).get("minted_for") or "another screen")
        return _json({"ok": False, "code": "form_key_subject_mismatch",
                      "error": f"This confirmation was issued for {minted_for}, not for "
                               f"{endpoint}. Nothing was done. Go back to that screen and "
                               f"confirm from its own figures."}, 409)
    if verdict.status == "in_progress":
        return _json({"ok": False, "error": "Already processing — do not resubmit."}, 409)
    if verdict.status != "ok":
        return _json({"ok": False,
                      "error": "This form has expired. Reload the page and try again."}, 409)

    try:
        body, status = await work(user, fields)
    except _Rejected as rej:
        # Provably moved nothing — hand the key back so the corrected form works.
        release_key(key)
        return _json({"ok": False, "error": rej.message}, rej.status)
    except Exception as e:
        # We do not know whether the engine moved money. The key stays CLAIMED:
        # a retry is refused rather than risking a second action.
        log.exception("[hub] %s failed after claim: %s", endpoint, e)
        return _json({"ok": False, "error": "The action failed. Check your balance "
                                            "before retrying — do not resubmit this form."}, 500)

    complete_key(key, body, status)
    return _json(body, status)


class _Rejected(Exception):
    """Validation failure that provably moved nothing. Releases the key."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


async def _read_fields(request: Any) -> dict:
    """Accept a JSON body or an HTML form post; both are the same shape here."""
    ctype = (request.headers.get("Content-Type") or "").lower()
    try:
        if "application/json" in ctype:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        post = await request.post()
        return {k: v for k, v in post.items()}
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════════

async def h_login(request: Any) -> Any:
    """Sign-in page. Two doors into one session store."""
    if current_user(request):
        raise web.HTTPFound(HUB_PREFIX)
    oauth = ""
    if oauth_enabled():
        oauth = (f'<a class="btn" href="{HUB_PREFIX}/auth/discord">Sign in with Discord</a>'
                 '<div class="foot">Discord tells us who you are. We never see your password '
                 'and we never ask you to type an id.</div>')
    else:
        oauth = ('<div class="notebox">Discord sign-in is not configured on this server '
                 '(DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET / DISCORD_REDIRECT_URI).</div>')
    body = f"""<div class="login-card">
<h1>Sign in</h1>
<p>The hub is transactional. Identity comes from your Discord session and is never
something you type into a form.</p>
{oauth}
<div class="section-h">Or use a login code</div>
<p>Run <code>/website_login</code> in Discord, then paste the code it gives you.</p>
<form method="post" action="{HUB_PREFIX}/login">
  <div class="amount-wrap"><span class="cn">#</span>
    <input class="amount" type="text" name="code" autocomplete="one-time-code"
           inputmode="numeric" placeholder="code" required></div>
  <button class="btn" type="submit">Sign in</button>
</form></div>"""
    return _html(page("V Tech Hub — sign in", "", None, None, body))


async def h_login_post(request: Any) -> Any:
    """Redeem a `/website_login` code against Restocker_web's existing store.

    Delegates to `_handle_api_link` rather than re-reading `web_login_codes.yml`:
    that file's shape belongs to the Discord command handler, which is not in
    this tree, and a second reader of it is a second thing to get wrong.
    """
    fields = await _read_fields(request)
    code = str(fields.get("code") or "").strip()
    if not code:
        raise web.HTTPFound(f"{HUB_PREFIX}/login")

    w = _web()

    class _Shim:
        """Minimal request the existing handler needs: a JSON body and an IP."""

        def __init__(self, real, payload):
            self._payload = payload
            self.headers = real.headers
            self.remote = getattr(real, "remote", None)
            self.cookies = real.cookies

        async def json(self):
            return self._payload

    resp = await w._handle_api_link(_Shim(request, {"code": code}))
    token = ""
    try:
        token = resp.cookies[SESSION_COOKIE].value       # type: ignore[index]
    except Exception:
        pass
    if not token:
        body = ('<div class="login-card"><h1>Sign in</h1>'
                '<div class="errbox">That code was not accepted. Codes are single-use — '
                'run <code>/website_login</code> again for a fresh one.</div>'
                f'<div class="row" style="margin-top:16px"><a class="btn ghost" '
                f'href="{HUB_PREFIX}/login">Back</a></div></div>')
        return _html(page("V Tech Hub — sign in", "", None, None, body), status=401)

    out = web.HTTPFound(HUB_PREFIX)
    _set_session_cookie(out, token)
    raise out


async def h_oauth_start(request: Any) -> Any:
    if not oauth_enabled():
        return _json({"ok": False, "error": "Discord sign-in is not configured."}, 503)
    state = secrets.token_urlsafe(24)
    q = urllib.parse.urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
        "prompt": "none",
    })
    resp = web.HTTPFound(f"{DISCORD_AUTHZ}?{q}")
    resp.set_cookie(OAUTH_STATE_COOKIE, state, httponly=True, secure=not _INSECURE_COOKIES,
                    max_age=600, samesite="Lax", path=HUB_PREFIX)
    raise resp


async def h_oauth_callback(request: Any) -> Any:
    """Exchange the code, read the Discord id, mint a session in the SHARED store.

    The Discord id is taken from Discord's own /users/@me response and from
    nowhere else — not from the query string, not from a body.
    """
    if not oauth_enabled():
        return _json({"ok": False, "error": "Discord sign-in is not configured."}, 503)
    want = request.cookies.get(OAUTH_STATE_COOKIE) or ""
    got = request.query.get("state") or ""
    if not want or not secrets.compare_digest(want, got):
        _record_attack("oauth_state_mismatch", request, None, "oauth/callback",
                       f"state={got[:12]}")
        return _json({"ok": False, "error": "Sign-in state mismatch. Start again."}, 400)
    code = request.query.get("code") or ""
    if not code:
        return _json({"ok": False, "error": "No authorization code."}, 400)

    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(DISCORD_TOKEN, data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
            if r.status != 200:
                log.warning("[hub oauth] token exchange %s: %s", r.status, await r.text())
                return _json({"ok": False, "error": "Discord rejected the sign-in."}, 502)
            tok = await r.json()
        async with s.get(DISCORD_ME, headers={
            "Authorization": f"{tok.get('token_type', 'Bearer')} {tok.get('access_token')}"
        }) as r:
            if r.status != 200:
                return _json({"ok": False, "error": "Could not read your Discord profile."}, 502)
            me = await r.json()

    uid = str(me.get("id") or "").strip()
    if not uid:
        return _json({"ok": False, "error": "Discord returned no user id."}, 502)
    name = me.get("global_name") or me.get("username") or uid

    token = _mint_session(uid, name)
    out = web.HTTPFound(HUB_PREFIX)
    _set_session_cookie(out, token)
    out.del_cookie(OAUTH_STATE_COOKIE, path=HUB_PREFIX)
    raise out


async def h_logout(request: Any) -> Any:
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        try:
            w = _web()
            w._SESSIONS.pop(tok, None)
            stored = w._load_sessions()
            if stored.pop(tok, None) is not None:
                w._save_sessions(stored)
        except Exception as e:
            log.warning("[hub] logout store cleanup failed: %s", e)
    out = web.HTTPFound(f"{HUB_PREFIX}/login")
    out.del_cookie(SESSION_COOKIE, path="/")
    raise out


async def h_me(request: Any) -> Any:
    """Identity + the strip's figures as JSON, for the mobile shell and for any
    section that wants the numbers without re-deriving them."""
    user = current_user(request)
    if not user:
        return _json({"logged_in": False}, 401)
    return _json({"logged_in": True, "user_id": user["user_id"], "name": user["name"],
                  "csrf": user["csrf"], "money": money_snapshot(user["user_id"])})


# ══════════════════════════════════════════════════════════════════════════
# MARKETS — reads. Every loader below is the bot's own; none of this maths is
# re-implemented here.
# ══════════════════════════════════════════════════════════════════════════

def _exchange_rows(uid: str) -> list[dict]:
    """Public listings joined with this user's holdings.

    Prices, changes, tickers and caps come from `Restocker_web._load_stock_data`,
    which is cached and stampede-proof; holdings come from
    `Restocker_db.get_portfolio`. Value is shares × the same share price the
    exchange shows — the same join `_handle_api_me` already does.
    """
    try:
        snap = _web()._load_stock_data() or {}
    except Exception as e:
        log.warning("[hub markets] stock snapshot failed: %s", e)
        return []
    mine: dict[str, dict] = {}
    try:
        for h in _db().get_portfolio(str(uid)):
            mine[str(h.get("market_id"))] = h
    except Exception as e:
        log.warning("[hub markets] portfolio read failed: %s", e)

    rows = []
    for m in snap.get("markets", []):
        mid = str(m.get("mid"))
        held = float((mine.get(mid) or {}).get("shares") or 0)
        price = float(m.get("price") or 0)
        rows.append({
            "mid": mid,
            "ticker": m.get("ticker") or mid.upper()[:4],
            "name": m.get("name") or mid,
            "price": price,
            "pct": float(m.get("pct") or 0),
            "shares": held,
            "value": held * price,
            "history": m.get("history") or [],
            "mcap": float(m.get("mcap") or 0),
        })
    rows.sort(key=lambda r: (-r["value"], -r["mcap"]))
    return rows


def _owned_markets(uid: str) -> list[str]:
    try:
        return list(_main()._owner_markets_for_user(str(uid)) or [])
    except Exception as e:
        log.warning("[hub markets] owner lookup failed: %s", e)
        return []


def _shop_rows(uid: str) -> list[dict]:
    """Shops this user owns, with 7-day takings from CSN transactions.

    Only the user's own shops: a stranger's revenue is not this page's business,
    and a "shop revenue" tile summed over markets you don't own is a meaningless
    figure.
    """
    owned = set(_owned_markets(uid))
    if not owned:
        return []
    try:
        inv = _web()._load_inventory_data() or {}
    except Exception as e:
        log.warning("[hub markets] inventory failed: %s", e)
        inv = {}
    fullness = {}
    names = {}
    for m in inv.get("markets", []):
        mid = str(m.get("mid") or m.get("id") or "")
        if not mid:
            continue
        names[mid] = m.get("name") or mid
        items = m.get("items") or []
        vals = []
        for it in items:
            cap = float(it.get("capacity") or 0)
            if cap > 0:
                vals.append(min(1.0, float(it.get("stock") or 0) / cap))
        fullness[mid] = {"items": len(items),
                         "pct": (sum(vals) / len(vals) * 100.0) if vals else None}

    d = _db()
    rows = []
    for mid in sorted(owned):
        income = 0.0
        txns = 0
        try:
            for day in d.get_csn_daily_sales(mid, 7) or []:
                income += float(day.get("income") or 0)
                txns += int(day.get("txns") or 0)
        except Exception as e:
            log.warning("[hub markets] daily sales %s: %s", mid, e)
            continue
        f = fullness.get(mid) or {}
        rows.append({"mid": mid, "name": names.get(mid, mid),
                     "income7": income, "txns7": txns,
                     "items": f.get("items"), "full_pct": f.get("pct")})
    return rows


def _hive_rows(uid: str) -> list[dict]:
    """Hive sites among this user's markets that have booked hive economics.

    A market with no `hive_ledger` rows is not a hive site and is left out — the
    mock's "4 hives, 2 need supers" has no source in this schema and is not
    invented here.
    """
    owned = _owned_markets(uid)
    if not owned:
        return []
    d = _db()
    try:
        names = {mid: (info.get("name") if isinstance(info, dict) else None) or mid
                 for mid, info in (_web()._load_markets() or {}).items()}
    except Exception:
        names = {}
    rows = []
    for mid in owned:
        try:
            months = d.get_hive_ledger_months(mid) or {}
        except Exception as e:
            log.warning("[hub markets] hive ledger %s: %s", mid, e)
            continue
        if not months:
            continue
        latest = sorted(months.keys())[-1]
        m = months[latest] or {}
        rows.append({
            "mid": mid, "name": names.get(mid, mid), "month": latest,
            "value": float(m.get("value") or 0),
            "harvester_pay": float(m.get("harvester_pay") or 0),
            "owner_pay": float(m.get("owner_pay") or 0),
            "net": float(m.get("net") or 0),
        })
    rows.sort(key=lambda r: -r["value"])
    return rows


def _sparkline(history: list[dict], width: int = 1120, height: int = 90) -> str:
    """Price history as an inline SVG polyline. Real points only — fewer than two
    and there is no line to draw, so the tile is omitted by the caller."""
    pts = [float(h.get("price") or 0) for h in history if h.get("price") is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    step = width / (len(pts) - 1)
    coords = " ".join(
        f"{i * step:.1f},{height - ((p - lo) / span) * (height - 8) - 4:.1f}"
        for i, p in enumerate(pts)
    )
    return (f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
            f'style="width:100%;height:{height}px;display:block" aria-hidden="true">'
            f'<polyline points="{coords}" fill="none" stroke="var(--green)" '
            f'stroke-width="2" vector-effect="non-scaling-stroke"/></svg>')


def _tile(label: str, value_html: str, sub: str) -> str:
    return (f'<div class="tile s4"><div class="tile-h" style="margin-bottom:6px">{esc(label)}</div>'
            f'<div class="num" style="font-size:24px;font-weight:600;letter-spacing:-.02em">{value_html}</div>'
            f'<div class="hold-s" style="margin-top:3px">{esc(sub)}</div></div>')


def _markets_body(uid: str) -> str:
    rows = _exchange_rows(uid)
    shops = _shop_rows(uid)
    hives = _hive_rows(uid)

    port = sum(r["value"] for r in rows)
    held_n = sum(1 for r in rows if r["shares"] > 0)

    tiles = [_tile("Portfolio value", cn(port),
                   f"{held_n} ticker{'s' if held_n != 1 else ''} held" if held_n
                   else "no holdings")]
    if shops:
        rev = sum(s["income7"] for s in shops)
        tiles.append(_tile("Shop takings · 7d", cn(rev),
                           f"{len(shops)} shop{'s' if len(shops) != 1 else ''} you own"))
    if hives:
        hv = sum(h["value"] for h in hives)
        month = hives[0]["month"]
        tiles.append(_tile(f"Hive value · {month}", cn(hv),
                           f"{len(hives)} hive site{'s' if len(hives) != 1 else ''}"))

    out = ['<div class="page-head"><div><h1>Markets</h1>'
           '<div class="page-sub">Shops, stock exchange and hives · V Tech core</div></div></div>',
           '<div class="bento">' + "".join(tiles) + '</div>']

    # Chart: the user's largest holding, else the largest listing. Real history
    # only — no line is drawn from a single point.
    lead = next((r for r in rows if r["shares"] > 0), rows[0] if rows else None)
    if lead:
        spark = _sparkline(lead["history"])
        if spark:
            arrow = "&#9650;" if lead["pct"] >= 0 else "&#9660;"
            cls = "good" if lead["pct"] >= 0 else "crit"
            out.append(
                '<div class="bento"><div class="tile s12"><div class="tile-h">'
                f'<span>{esc(lead["ticker"])} · {esc(lead["name"])} — '
                f'last {len(lead["history"])} price points</span>'
                '<span class="row" style="gap:10px">'
                f'<span class="num" style="font-size:19px;font-weight:600;color:var(--text)">'
                f'{px(lead["price"])}<span class="coin">c</span></span>'
                f'<span class="pill {cls}">{arrow} {lead["pct"]:+.1f}%</span></span></div>'
                f'{spark}<div class="chart-legend" style="margin-top:6px">'
                f'<span class="lg"><i style="background:#22FF7A"></i>'
                f'{esc(lead["ticker"])} mid price</span></div></div></div>')

    out.append('<div class="section-h">Exchange</div>')
    if not rows:
        out.append('<div class="empty">No public listings.</div>')
    else:
        trs = []
        for r in rows:
            cls = "up" if r["pct"] >= 0 else "down"
            holding = n(r["shares"]) if r["shares"] else '<span class="muted">&mdash;</span>'
            value = cn(r["value"]) if r["shares"] else '<span class="muted">&mdash;</span>'
            trs.append(
                f'<tr><td><a href="{HUB_PREFIX}/markets/{esc(r["mid"])}">'
                f'<span class="tick">{esc(r["ticker"])}</span>'
                f'<div class="tname">{esc(r["name"])}</div></a></td>'
                f'<td class="num">{px(r["price"])}</td>'
                f'<td class="num {cls}">{r["pct"]:+.1f}%</td>'
                f'<td class="num hide-sm">{holding}</td>'
                f'<td class="num">{value}</td>'
                f'<td><a class="btn ghost sm" href="{HUB_PREFIX}/markets/{esc(r["mid"])}">Trade</a></td></tr>')
        out.append('<div class="tablewrap"><table><thead><tr>'
                   '<th>Ticker</th><th>Price</th><th>24h</th>'
                   '<th class="hide-sm">Holding</th><th>Value</th><th></th>'
                   '</tr></thead><tbody>' + "".join(trs) + '</tbody></table></div>')
        out.append('<div class="foot">Prices are the exchange mid. A trade fills at the '
                   'average of the pre- and post-impact price plus the spread, so the fill '
                   'you are quoted on the confirm screen is not the mid above. Trades settle '
                   'against the core wallet — nothing is held here.</div>')

    if shops:
        out.append('<div class="section-h">Your shops</div>')
        trs = []
        for s in shops:
            full = (f'<span class="num">{s["full_pct"]:.0f}%</span>'
                    if s["full_pct"] is not None else '<span class="muted">&mdash;</span>')
            items = n(s["items"]) if s["items"] is not None else '<span class="muted">&mdash;</span>'
            trs.append(f'<tr><td>{esc(s["name"])}</td>'
                       f'<td class="num">{items}</td><td>{full}</td>'
                       f'<td class="num">{cn(s["income7"])}</td>'
                       f'<td class="num">{n(s["txns7"])}</td></tr>')
        out.append('<div class="tablewrap"><table><thead><tr><th>Shop</th>'
                   '<th>Items</th><th>Barrels full</th><th>Takings 7d</th><th>Sales 7d</th>'
                   '</tr></thead><tbody>' + "".join(trs) + '</tbody></table></div>')

    if hives:
        out.append('<div class="section-h">Hives</div>')
        trs = []
        for h in hives:
            trs.append(f'<tr><td>{esc(h["name"])}<div class="tname">{esc(h["month"])}</div></td>'
                       f'<td class="num">{cn(h["value"])}</td>'
                       f'<td class="num">{cn(h["harvester_pay"])}</td>'
                       f'<td class="num">{cn(h["owner_pay"])}</td>'
                       f'<td class="num">{cn(h["net"])}</td></tr>')
        out.append('<div class="tablewrap"><table><thead><tr><th>Site</th>'
                   '<th>Harvest value</th><th>Harvester pay</th><th>Owner pay</th><th>Net</th>'
                   '</tr></thead><tbody>' + "".join(trs) + '</tbody></table></div>')
        out.append('<div class="foot">Hive values are per piece and already booked by the '
                   'hive engine; this reads the same ledger the monthly roll-up uses.</div>')

    return "".join(out)


async def h_markets(request: Any) -> Any:
    user = current_user(request)
    if not user:
        return _login_required_page(request)
    snap = money_snapshot(user["user_id"])
    return _html(page("Markets · V Tech Hub", "markets", user, snap,
                      _markets_body(user["user_id"])))


async def h_api_markets(request: Any) -> Any:
    user = current_user(request)
    if not user:
        return _json({"ok": False, "error": "Log in first."}, 401)
    rows = _exchange_rows(user["user_id"])
    for r in rows:
        r.pop("history", None)          # the table doesn't need 5000 points
    return _json({"ok": True, "markets": rows})


# ── one market: quote, holding, and the trade form ────────────────────────

def _listing(mid: str) -> Optional[dict]:
    try:
        return _db().get_market_shares(mid)
    except Exception as e:
        log.warning("[hub markets] listing %s: %s", mid, e)
        return None


def _quote(side: str, mid: str, shares: int) -> dict:
    """Indicative fill for `shares`, priced by the BOT'S OWN `_quote_trade`.

    This is the one function that must never be re-derived here: a second
    slippage/spread formula that disagrees with the engine's would quote a
    number the trade then doesn't honour. The result is still indicative — the
    engine re-reads price and supply inside the bot loop at submit time.
    """
    listing = _listing(mid) or {}
    price = float(listing.get("share_price") or 0)
    shares_out = float(listing.get("shares_outstanding") or 0)
    fill, new_mid = _main()._quote_trade(price, shares, shares_out, side)
    total = int(round(fill * shares))
    return {"price": price, "fill": fill, "total": total, "new_mid": new_mid,
            "shares_out": shares_out}


def _available_shares(mid: str, shares_out: float) -> float:
    try:
        held = sum(float(h.get("shares") or 0) for h in _db().get_holders(mid))
    except Exception:
        return shares_out
    return shares_out - held


def _my_shares(uid: str, mid: str) -> float:
    try:
        h = _db().get_holding(str(uid), mid)
    except Exception:
        return 0.0
    return float((h or {}).get("shares") or 0)


def _market_body(user: dict, snap: dict, mid: str, side: str = "buy",
                 confirm: Optional[dict] = None, error: str = "",
                 result: Optional[dict] = None) -> str:
    uid = user["user_id"]
    listing = _listing(mid)
    if not listing:
        return ('<div class="page-head"><div><h1>Not listed</h1>'
                '<div class="page-sub">No such stock on the exchange.</div></div></div>')

    name = ""
    try:
        info = (_web()._load_markets() or {}).get(mid) or {}
        name = (info.get("name") if isinstance(info, dict) else None) or mid
        lbl = str(_db().get_config(f"stock_label:{mid}") or "").strip()
        if lbl:
            name = lbl
    except Exception:
        name = mid
    ticker = mid.upper()[:4]
    try:
        ticker = _web()._market_ticker(mid)
    except Exception:
        pass

    price = float(listing.get("share_price") or 0)
    shares_out = float(listing.get("shares_outstanding") or 0)
    mine = _my_shares(uid, mid)
    free = _available_shares(mid, shares_out)
    active = bool(listing.get("active"))

    out = [f'<div class="page-head"><div><h1>{esc(name)}</h1>'
           f'<div class="page-sub"><span class="tick">{esc(ticker)}</span> · '
           f'{esc(mid)} · stock exchange</div></div>'
           f'<div style="margin-left:auto"><a class="btn ghost sm" '
           f'href="{HUB_PREFIX}/markets">All markets</a></div></div>']

    out.append('<div class="bento">'
               + _tile("Mid price", f'{px(price)}<span class="coin">c</span>',
                       "exchange quote, pre-impact")
               + _tile("You hold", n(mine), f"{n(mine * price)}c at the mid")
               + _tile("Unheld shares", n(free), f"of {n(shares_out)} outstanding")
               + '</div>')

    if result is not None:
        cls = "okbox" if result.get("ok") else "errbox"
        extra = ""
        if result.get("ok"):
            extra = (f'<div class="kv"><span class="k">Filled</span>'
                     f'<span class="v">{n(result.get("shares"))} shares at '
                     f'{px(result.get("fill"))}c</span></div>'
                     f'<div class="kv"><span class="k">Total</span>'
                     f'<span class="v">{cn(result.get("total"))}</span></div>')
        out.append(f'<div class="{cls}">{esc(result.get("message") or "")}{extra}</div>')

    if error:
        out.append(f'<div class="errbox">{esc(error)}</div>')

    if not active:
        out.append('<div class="notebox">This stock is delisted. Holdings are frozen '
                   'until it goes public again.</div>')
        return "".join(out)

    if confirm:
        # Preview-then-confirm: the figures are on the same screen as the button.
        avail = snap.get("available")
        if avail is None:
            # No wallet figure means no honest "after" figure either.
            avail_row = ('<div class="kv"><span class="k">Available</span>'
                         '<span class="v muted">unavailable</span></div>')
        else:
            after = (avail - confirm["total"]) if confirm["side"] == "buy" else (avail + confirm["total"])
            avail_row = (f'<div class="kv"><span class="k">Available now</span>'
                         f'<span class="v">{cn(avail)}</span></div>'
                         f'<div class="kv"><span class="k">Available after</span>'
                         f'<span class="v">{cn(after)}</span></div>')
        out.append(f"""
<div class="section-h">Confirm</div>
<div class="tile s12" style="max-width:520px">
  <div class="tile-h"><span>{esc(confirm["side"].upper())} {n(confirm["shares"])} {esc(ticker)}</span>
    <span class="pill warn">indicative</span></div>
  <div class="kv"><span class="k">Shares</span><span class="v">{n(confirm["shares"])}</span></div>
  <div class="kv"><span class="k">Fill per share</span><span class="v">{px(confirm["fill"])}<span class="coin">c</span></span></div>
  <div class="kv"><span class="k">Mid before</span><span class="v">{px(confirm["price"])}<span class="coin">c</span></span></div>
  <div class="kv"><span class="k">Mid after</span><span class="v">{px(confirm["new_mid"])}<span class="coin">c</span></span></div>
  <div class="kv total"><span class="k">{"You pay" if confirm["side"] == "buy" else "You receive"}</span>
    <span class="v">{cn(confirm["total"])}</span></div>
  {avail_row}
  <form method="post" action="{HUB_PREFIX}/markets/{esc(mid)}/trade" style="margin-top:16px">
    <input type="hidden" name="csrf" value="{esc(user["csrf"])}">
    <input type="hidden" name="idempotency_key" value="{esc(confirm["key"])}">
    <input type="hidden" name="side" value="{esc(confirm["side"])}">
    <input type="hidden" name="shares" value="{int(confirm["shares"])}">
    <div class="row wrapf">
      <button class="btn" type="submit">Confirm {esc(confirm["side"])}</button>
      <a class="btn ghost" href="{HUB_PREFIX}/markets/{esc(mid)}">Cancel</a>
    </div>
  </form>
  <div class="notebox">These figures are indicative. Price, supply and your balance are
  re-read on the bot loop when you confirm; if the market moved, the fill you get is the
  one priced at that moment. This form can only be submitted once.</div>
</div>""")
        return "".join(out)

    buy_sel = ' checked' if side != "sell" else ''
    sell_sel = ' checked' if side == "sell" else ''
    out.append(f"""
<div class="section-h">Trade</div>
<form class="tile s12" style="max-width:520px" method="post"
      action="{HUB_PREFIX}/markets/{esc(mid)}/preview">
  <input type="hidden" name="csrf" value="{esc(user["csrf"])}">
  <div class="side-pick">
    <label><input type="radio" name="side" value="buy"{buy_sel}><span>Buy</span></label>
    <label><input type="radio" name="side" value="sell"{sell_sel}><span>Sell</span></label>
  </div>
  <div class="amount-wrap"><span class="cn">#</span>
    <input class="amount" type="number" name="shares" min="1" max="{MAX_SHARES_PER_TRADE}"
           step="1" placeholder="shares" required></div>
  <button class="btn" type="submit">Preview</button>
  <div class="foot">Nothing moves until you confirm on the next screen, where you will see
  the fill price and the total.</div>
</form>""")
    return "".join(out)


async def h_market(request: Any) -> Any:
    user = current_user(request)
    if not user:
        return _login_required_page(request)
    mid = request.match_info["mid"]
    snap = money_snapshot(user["user_id"])
    return _html(page(f"{mid} · Markets · V Tech Hub", "markets", user, snap,
                      _market_body(user, snap, mid)))


def _parse_shares(fields: dict) -> int:
    try:
        shares = int(str(fields.get("shares") or "0").strip())
    except (TypeError, ValueError):
        raise _Rejected("Shares must be a whole number.")
    if not (1 <= shares <= MAX_SHARES_PER_TRADE):
        raise _Rejected(f"Shares must be 1..{MAX_SHARES_PER_TRADE:,}.")
    return shares


def _parse_side(fields: dict) -> str:
    side = str(fields.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        raise _Rejected("Side must be buy or sell.")
    return side


async def h_market_preview(request: Any) -> Any:
    """Price the trade and render the confirm screen. Moves nothing, so it mints
    the key rather than spending one."""
    user = current_user(request)
    if not user:
        return _login_required_page(request)
    mid = request.match_info["mid"]
    fields = await _read_fields(request)
    _scan_body_identity(fields, request, user["user_id"], "markets/preview")
    snap = money_snapshot(user["user_id"])

    if not _csrf_ok(request, user, str(fields.get("csrf") or "")):
        return _html(page("Markets · V Tech Hub", "markets", user, snap,
                          _market_body(user, snap, mid, error="Session check failed. Reload and try again.")),
                     status=403)
    try:
        side = _parse_side(fields)
        shares = _parse_shares(fields)
    except _Rejected as rej:
        return _html(page("Markets · V Tech Hub", "markets", user, snap,
                          _market_body(user, snap, mid, error=rej.message)), status=400)

    q = _quote(side, mid, shares)
    # The key names the TICKER it was priced against. A bare "markets/trade" key was
    # spendable on any listing on the exchange, so the figures on this confirm screen
    # bound nothing (WEB_ATTACK finding 7). `h_market_trade` claims the same string,
    # built from its own route's `mid`, so a key from another ticker's confirm screen
    # cannot book this one.
    confirm = {"side": side, "shares": shares,
               "key": mint_key(user["user_id"], f"markets/trade:{mid}"), **q}
    return _html(page(f"Confirm · {mid} · V Tech Hub", "markets", user, snap,
                      _market_body(user, snap, mid, side, confirm=confirm)))


async def h_market_trade(request: Any) -> Any:
    """Book the trade. Exactly once per minted key.

    The engine is `Restocker_main._do_stock_trade`, marshalled onto the bot loop:
    its supply check and its writes are not atomic, and only that shared loop
    keeps a web trade from interleaving with a Discord one.
    """
    mid = request.match_info["mid"]

    async def work(user: dict, fields: dict):
        side = _parse_side(fields)
        shares = _parse_shares(fields)
        listing = _listing(mid)
        if not listing:
            raise _Rejected(f"{mid} is not on the exchange.", 404)
        if not listing.get("active") and side == "buy":
            raise _Rejected(f"{mid} is delisted.", 409)

        m = _main()
        r = await m.run_on_bot_loop(m._do_stock_trade, side, user["user_id"], mid,
                                    shares, user.get("name") or "")
        body = {
            "ok": bool(r.get("ok")),
            "code": r.get("code"),
            "message": r.get("msg"),
            "side": side,
            "market_id": mid,
            "shares": r.get("shares"),
            "fill": r.get("fill"),
            "total": r.get("total"),
            "new_price": r.get("new_price"),
        }
        return body, (200 if r.get("ok") else 409)

    resp = await idempotent_post(request, f"markets/trade:{mid}", work)

    # A browser form post wants a page; a fetch caller wants the JSON it already got.
    accepts = (request.headers.get("Accept") or "")
    if "application/json" in accepts or "text/html" not in accepts:
        return resp
    user = current_user(request)
    if not user:
        return _login_required_page(request)
    try:
        result = json.loads(resp.text or "{}")
    except Exception:
        result = {"ok": False, "message": "Unreadable result."}
    if not result.get("message"):
        result["message"] = result.get("error") or ""
    snap = money_snapshot(user["user_id"])
    return _html(page(f"{mid} · Markets · V Tech Hub", "markets", user, snap,
                      _market_body(user, snap, mid, result=result)),
                 status=200 if result.get("ok") else resp.status)


# ══════════════════════════════════════════════════════════════════════════
# Mount
# ══════════════════════════════════════════════════════════════════════════

async def h_root(request: Any) -> Any:
    """`/hub` lands on the first registered section. When only Markets is
    registered that is Markets; when the Hub section registers itself it becomes
    the landing page without this file changing."""
    if not current_user(request):
        return _login_required_page(request)
    if _SECTIONS:
        raise web.HTTPFound(_SECTIONS[0]["path"])
    raise web.HTTPFound(f"{HUB_PREFIX}/markets")


async def h_health(request: Any) -> Any:
    """Public probe. Says the hub is mounted and how it can be signed into —
    never who is signed in."""
    return _json({"ok": True, "service": "vtech-hub", "version": HUB_VERSION,
                  "oauth": oauth_enabled(),
                  "sections": [s["key"] for s in _SECTIONS], "ts": time.time()})


register_section("markets", "Markets", f"{HUB_PREFIX}/markets", order=20)


def register_hub_routes(app: Any) -> None:
    """Attach the hub to an existing aiohttp Application.

    Call from `Restocker_web.start_webserver()` next to `register_bank_routes`.
    """
    if web is None:                      # pragma: no cover
        log.warning("[hub] aiohttp unavailable — hub not registered.")
        return
    routes = [
        ("get",  "",                          h_root),
        ("get",  "/",                         h_root),
        ("get",  "/health",                   h_health),
        ("get",  "/login",                    h_login),
        ("post", "/login",                    h_login_post),
        ("get",  "/auth/discord",             h_oauth_start),
        ("get",  "/auth/discord/callback",    h_oauth_callback),
        ("post", "/logout",                   h_logout),
        ("get",  "/api/me",                   h_me),
        ("get",  "/markets",                  h_markets),
        ("get",  "/api/markets",              h_api_markets),
        ("get",  "/markets/{mid}",            h_market),
        ("post", "/markets/{mid}/preview",    h_market_preview),
        ("post", "/markets/{mid}/trade",      h_market_trade),
    ]
    for method, path, handler in routes:
        full = HUB_PREFIX + path
        if method == "get":
            app.router.add_get(full, handler)
        else:
            app.router.add_post(full, handler)
    log.info("[hub] routes registered under %s — sections: %s",
             HUB_PREFIX, ", ".join(s["key"] for s in _SECTIONS))
    print(f"V Tech Hub v{HUB_VERSION} mounted at {HUB_PREFIX}/ "
          f"(sections: {', '.join(s['key'] for s in _SECTIONS)}; "
          f"discord oauth: {'on' if oauth_enabled() else 'off'})")
