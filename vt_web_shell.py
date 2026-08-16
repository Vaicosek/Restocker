"""
vt_web_shell.py — shared chrome, identity and form-idempotency for the V Tech
transactional site (`banking_web.py`, `estates_web.py`).

WHY THIS FILE EXISTS
--------------------
Both new sections render the same approved design (`/home/claude/build/mock_*.html`)
and both take money instructions from a browser. Duplicating 17KB of stylesheet and,
worse, duplicating the identity and idempotency rules is how the two halves drift
until one of them is wrong about who the user is. There is one copy, here.

THE FOUR RULES THIS MODULE ENFORCES, so the section modules cannot get them wrong
--------------------------------------------------------------------------------
1. **Identity is the session cookie and nothing else.** `session_user(request)` is the
   only `whoami`. It delegates to `Restocker_web._session_user` — the real one, reading
   the `vtm_sess` HttpOnly cookie — and FAILS CLOSED (nobody logged in) if that module
   cannot be imported. A `user_id` in a request body is never read; `note_body_identity`
   logs it as an attack signal and the caller carries on with the session id.
2. **CSRF on every state change.** `csrf_ok(request)` is the same contract the existing
   owner POSTs use: `X-CSRF-Token` must equal the session's `csrf`, which the browser
   learned from `GET /api/me`.
3. **Every money form carries an idempotency key minted when the page renders.**
   `mint_form_key(user_id, purpose)` mints one; it is HMAC-signed over
   (purpose, user id, timestamp), so the server can prove at submit time that it minted
   this key, for THIS user, for THIS action, within the last `FORM_KEY_TTL` seconds —
   without storing anything at render. `claim_form_key` then makes it single-use, and a
   replay returns the stored original response instead of acting twice.
4. **Nothing here mints, moves or holds a coin.** The shell renders and authenticates.
   Money lives in `ledger_v2` (estates, in-process) and Osentar (banking, over HTTP).

THE SHARED WALLET STRIP
-----------------------
`GET /api/wallet/strip` is the one route this module owns, because the strip is on every
page and its three figures come from two different services. `available` and `held` come
from `ledger_v2` in-process; `savings` and `loan outstanding` come from Osentar over HTTP
via `banking_web` (imported lazily, inside the handler — there is no import cycle and no
hard dependency). If Osentar is down the strip still renders: available and held are
real, savings reads `unavailable`, and `net` is withheld rather than computed from a
number we do not have. A net position that silently drops savings is a lie.

Registration follows the `bank_api.register_bank_routes` pattern exactly:

    import vt_web_shell, banking_web, estates_web
    vt_web_shell.register_shell_routes(app)
    banking_web.register_banking_routes(app)
    estates_web.register_estates_routes(app)

immediately before `web.AppRunner` in `Restocker_web.start_webserver`.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from typing import Any, Callable, Optional

try:
    from aiohttp import web
except Exception:  # pragma: no cover - aiohttp is a hard dep of the web server
    web = None  # type: ignore[assignment]

log = logging.getLogger("vt_web_shell")

SHELL_VERSION = "1.0"

#: Seconds a rendered money form stays submittable. Long enough to read a preview
#: and think about it, short enough that a key scraped off a stale page is dead.
FORM_KEY_TTL = 6 * 3600

#: Signing secret for form keys. Unset -> a per-process random, which is correct
#: rather than convenient: after a restart every open form is refused with
#: "this form expired, reload the page" instead of being silently accepted.
_FORM_SECRET = (os.getenv("VT_WEB_KEY_SECRET", "").strip()
                or secrets.token_urlsafe(32)).encode("utf-8")

#: Staff are named, in one place, and the name is a Discord id. `VT_STAFF_IDS` is a
#: comma-separated list; `web_staff_ids` in the bot's config table is the same list
#: editable without a redeploy. A non-staff session does not get a disabled staff
#: panel — it does not get the panel, and it does not get the routes either.
_STAFF_ENV = "VT_STAFF_IDS"
_STAFF_CONFIG_KEY = "web_staff_ids"


# ══════════════════════════════════════════════════════════════════════════
# Identity — the session cookie, and nothing else
# ══════════════════════════════════════════════════════════════════════════

#: Test seam. `Restocker_web` pulls in the whole bot at import; an aiohttp test
#: client sets this to a plain dict-backed reader instead. Production never
#: touches it, and when it is None the resolver below uses the real thing.
_session_provider: Optional[Callable[[Any], Optional[dict]]] = None


def set_session_provider(fn: Optional[Callable[[Any], Optional[dict]]]) -> None:
    """Install an alternative session reader. FOR TESTS ONLY.

    Production leaves this None so `session_user` resolves `Restocker_web._session_user`,
    which is the deployed cookie/session store. A test harness that cannot import the
    bot installs a reader over the same `vtm_sess` cookie so the handlers under test see
    exactly the identity path they will see in production.
    """
    global _session_provider
    _session_provider = fn


def session_user(request) -> Optional[dict]:
    """`{user_id, name, csrf}` for this request's session cookie, or None.

    THE ONLY IDENTITY FUNCTION. Every handler in `banking_web` and `estates_web` starts
    here. If `Restocker_web` cannot be imported this returns None for everybody — the
    site is logged-out, not open. Failing closed is the only acceptable direction for a
    surface where a POST spends coins.
    """
    if _session_provider is not None:
        return _session_provider(request)
    try:
        import Restocker_web as _rw
    except Exception as e:  # pragma: no cover - only when the bot is absent
        log.error("[vt_web] identity unavailable (%s) — every session reads as logged out", e)
        return None
    try:
        return _rw._session_user(request)
    except Exception as e:
        log.exception("[vt_web] session lookup failed: %s", e)
        return None


def note_body_identity(request, body: Any, sess: dict) -> None:
    """A user id in a request body is IGNORED and logged as an attack signal.

    It is never an accident: no page this codebase renders puts a user id in a money
    payload, so a body carrying one is either a hand-rolled client or somebody trying to
    act as another player. The session id is what the handler uses either way — this
    function has no return value on purpose, so there is no branch a caller can get
    backwards.
    """
    if not isinstance(body, dict):
        return
    claimed = None
    for field in ("user_id", "uid", "discord_id", "actor_id", "on_behalf_of", "from_user"):
        if body.get(field) not in (None, ""):
            claimed = f"{field}={body.get(field)!r}"
            break
    if claimed is None:
        return
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or (getattr(request, "remote", None) or "unknown"))
    log.warning("[vt_web][ATTACK-SIGNAL] body-supplied identity ignored: path=%s "
                "session_user=%s claimed=%s ip=%s",
                request.path, sess.get("user_id"), claimed, ip)


def csrf_ok(request) -> bool:
    """State-changing POSTs carry the session's CSRF token in `X-CSRF-Token`.

    Same contract as the existing owner POSTs in `Restocker_web` (`_csrf_ok`, :3302),
    on top of the cookie's SameSite=Lax. Read-only GETs do not need it.
    """
    sess = session_user(request)
    if not sess:
        return False
    want = sess.get("csrf") or ""
    got = request.headers.get("X-CSRF-Token", "")
    return bool(want) and hmac.compare_digest(str(want), str(got))


def staff_ids() -> set:
    """The staff allowlist, from `VT_STAFF_IDS` and the bot's `web_staff_ids` config."""
    out = {p.strip() for p in os.getenv(_STAFF_ENV, "").replace(";", ",").split(",") if p.strip()}
    try:
        import Restocker_db as _db
        raw = _db.get_config(_STAFF_CONFIG_KEY) or ""
        out |= {p.strip() for p in str(raw).replace(";", ",").split(",") if p.strip()}
    except Exception:
        pass
    return out


def is_staff(sess: Optional[dict]) -> bool:
    if not sess:
        return False
    return str(sess.get("user_id")) in staff_ids()


# ══════════════════════════════════════════════════════════════════════════
# Responses — one JSON convention for both sections
# ══════════════════════════════════════════════════════════════════════════

def json_ok(**payload) -> Any:
    return web.json_response({"ok": True, **payload})


def json_err(code: str, message: str, status: int = 400, **extra) -> Any:
    """Every refusal is machine-readable (`code`) AND human (`error`).

    `code` is what the page branches on; `error` is what it shows. A refusal that only
    has prose forces the client to string-match, and a refusal that only has a code
    gets rendered to a player as a code.
    """
    return web.json_response({"ok": False, "code": code, "error": message, **extra},
                             status=status)


def require_session(request):
    """`(sess, None)` when logged in, `(None, response)` when not.

    A logged-out POST is refused here, before any body is read, with 401 and a code the
    page turns into the login prompt.
    """
    sess = session_user(request)
    if not sess:
        return None, json_err("not_logged_in",
                              "Log in first — run /website_login in Discord.", 401)
    return sess, None


def login_page(request) -> Any:
    """401 + the sign-in card, for a logged-out request to a whole PAGE.

    Mirrors `hub_web._login_required_page` exactly, including the status code: a section
    of this site is somebody's own money, so a logged-out visitor gets a sign-in card
    and NOT a chrome-and-dashes render of a page whose every figure is an error box.
    401 rather than 200 so a monitor, a crawler and a cache all agree this was not a
    page. No nav, no money strip, no section body — nothing that has to be told later
    that it was never real.
    """
    ways = []
    try:
        import hub_web as _hub
        prefix = _hub.HUB_PREFIX
        if _hub.oauth_enabled():
            ways.append(f'<a class="btn" href="{prefix}/auth/discord">Sign in with Discord</a>')
        ways.append(f'<a class="btn ghost" href="{prefix}/login">Use a login code</a>')
    except Exception:  # pragma: no cover - hub absent in a bare embed
        pass
    body = ('<div class="tile s12"><div class="tile-h">Sign in</div>'
            '<p style="font-size:12.5px;color:var(--text-body);max-width:52ch">'
            'Every page in this section is your own money, so nothing renders until we '
            'know who you are. Run <span class="mono">/website_login</span> in Discord '
            'to get a code.</p>'
            f'<div class="row" style="margin-top:14px;display:flex;gap:8px">{"".join(ways)}</div>'
            '</div>')
    resp = page("Sign in", "", body, "", strip=False)
    resp.set_status(401)
    return resp


def require_page_session(request):
    """`(sess, None)` when logged in, `(None, login_page)` when not. For GET page routes.

    The JSON API is gated by `require_session`; this is the same gate one level up, for
    the HTML shell itself. Both `banking_web.h_page` and `estates_web.h_page` start here.
    """
    sess = session_user(request)
    if not sess:
        return None, login_page(request)
    return sess, None


def require_post_session(request):
    """Session + CSRF, the two checks every money POST starts with."""
    sess, refusal = require_session(request)
    if refusal is not None:
        return None, refusal
    if not csrf_ok(request):
        return None, json_err("bad_csrf", "Bad or missing CSRF token. Reload the page.", 403)
    return sess, None


async def read_json(request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def coins(value: Any, field: str = "amount") -> int:
    """Integer coins or a refusal. Floats, NaN and strings-with-commas all die here.

    `int(float('nan'))` raises one line past every guard that failed open — which is the
    documented land-exchange NaN path (LAND_ESCROW_PLAN §2.2). So NaN and infinity are
    rejected explicitly and by name, not left to a downstream cast.
    """
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a whole number of coins")
    if isinstance(value, str):
        value = value.replace(",", "").replace(" ", "").strip()
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a whole number of coins")
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError(f"{field} must be a whole number of coins")
    i = int(f)
    if i != f:
        raise ValueError(f"{field} must be a whole number of coins — no fractions")
    return i


# ══════════════════════════════════════════════════════════════════════════
# Form idempotency — minted at render, signed, single-use at submit
# ══════════════════════════════════════════════════════════════════════════

#: For the wording of a subject-mismatch refusal: `bid` -> "lot", so the message can
#: say "a different lot" instead of "a different action". Purely cosmetic; the refusal
#: itself is a string comparison and needs no entry here.
_SUBJECT_NOUN = {
    "bid": "lot",
    "stake": "market outcome",
    "bond_buy": "bond term",
    "bond_redeem": "bond",
    "staff_decide": "loan request",
    "staff_collect": "loan",
}


class UnkeyableSubject(ValueError):
    """A purpose that cannot be signed unambiguously, refused AT MINT TIME.

    The subject half of a purpose comes from Osentar (a bond id) or from a lot row —
    not from us. Two characters would make the key ambiguous rather than merely ugly:
    `"|"` is the field separator inside the signed message, so a subject containing one
    could be re-split into a different (purpose, user_id) pair that signs identically;
    control characters would travel through headers and logs as something other than
    what was signed. Dots are NOT in this list — the key is split from the right, so a
    subject may contain them (`bond_redeem:B.201` is a real, spendable key).

    Refusing here is deliberate. The alternative — mapping every unkeyable subject onto
    a placeholder like `"?"` — makes two different bad subjects into one purpose, which
    is a key minted for one thing that spends on another. That is the bug the subject
    was added to close.
    """


#: What a purpose may not contain: the signing separator, and anything unprintable.
_PURPOSE_FORBIDDEN = ("|",)
#: A subject longer than this is not an id, it is an attempt at something.
PURPOSE_MAX = 120


def check_purpose(purpose: str) -> str:
    """Return `purpose` if it can be signed unambiguously; raise `UnkeyableSubject`."""
    p = str(purpose or "")
    if not p:
        raise UnkeyableSubject("a form key needs a purpose")
    if len(p) > PURPOSE_MAX:
        raise UnkeyableSubject(f"form key purpose is too long ({len(p)} chars)")
    bad = [c for c in p if c in _PURPOSE_FORBIDDEN or ord(c) < 0x20 or ord(c) == 0x7F]
    if bad:
        raise UnkeyableSubject(
            "form key purpose contains a character that cannot be signed "
            f"unambiguously: {sorted(set(bad))!r}")
    return p


def mint_form_key(user_id: str, purpose: str) -> str:
    """Mint the key a money form carries. Called WHEN THE PAGE RENDERS.

    Shape: `<purpose>.<issued_at>.<nonce>.<sig>` where sig is
    HMAC-SHA256(secret, purpose|user_id|issued_at|nonce) truncated to 128 bits.

    THE KEY IS SPLIT FROM THE RIGHT, and that is load-bearing. `issued`, `nonce`
    (`token_urlsafe`) and `sig` (hex) can never contain a dot, so `rsplit(".", 3)`
    recovers exactly four fields no matter what the purpose holds. Splitting from the
    left made any subject with a dot in it — and bond ids come from Osentar, not from
    us — mint a key that could never be spent: `bond_redeem:B.201` verified as
    "this form is missing its confirmation key" (WEB_VERIFY_R2 NEW-3). Encoding the
    subject was the alternative; splitting from the right keeps existing keys valid and
    keeps the purpose readable in a log line, which is worth more than escaping rules.

    THE PURPOSE CARRIES THE SUBJECT. `"bid:41"`, not `"bid"`; `"bond_redeem:B-201"`,
    not `"bond_redeem"`. A purpose that names only the action lets a key minted while
    the player read lot 3's figures commit a bid on lot 4 — the preview-then-confirm
    promise is then only as good as the browser, which is WEB_ATTACK finding 7. The
    convention is `<action>:<subject>`, the subject is whatever the previewed figures
    were about, and every mint site has a verify site passing the same string.

    Signing rather than storing means the render path writes no rows — a page with
    forty lots on it mints forty keys and touches the database zero times — while
    `verify_form_key` can still prove at submit time that the server minted this exact
    key, for this user, for this action, ON THIS SUBJECT, recently. The row is only
    written when the key is actually used, by `claim_form_key`.
    """
    purpose = check_purpose(purpose)
    issued = str(int(time.time()))
    nonce = secrets.token_urlsafe(12)
    sig = _sign(purpose, str(user_id), issued, nonce)
    return f"{purpose}.{issued}.{nonce}.{sig}"


def _sign(purpose: str, user_id: str, issued: str, nonce: str) -> str:
    msg = "|".join((purpose, user_id, issued, nonce)).encode("utf-8")
    return hmac.new(_FORM_SECRET, msg, hashlib.sha256).hexdigest()[:32]


def form_key_subject(key: str) -> str:
    """The subject a key CLAIMS, unverified — `"B-201"` from `bond_redeem:B-201...`.

    For filing a key against the row it belongs to at render time, never for deciding
    whether it may be spent. That decision is `verify_form_key`, which checks the
    signature; this reads a string an attacker could have written.

    Split from the right (see `mint_form_key`): the subject may contain dots.
    """
    parts = str(key or "").rsplit(".", 3)
    head = parts[0] if len(parts) == 4 else ""
    return head.split(":", 1)[1] if ":" in head else ""


def is_subject_mismatch(key: str, user_id: str, purpose: str) -> bool:
    """True when `key` is a REAL key of ours, for this user and this action, minted
    for a DIFFERENT SUBJECT than `purpose` names.

    The signature is checked before saying so on purpose. Without that check a forged
    key that simply starts with `"bid"` would be reported as "you confirmed the wrong
    lot", which tells an attacker that the string before the first dot is parsed and
    tells an honest player something untrue about their own page. A subject mismatch is
    only a subject mismatch if we minted the thing.
    """
    parts = str(key or "").rsplit(".", 3)
    if len(parts) != 4 or ":" not in str(purpose):
        return False
    got_purpose, issued, nonce, sig = parts
    if got_purpose == purpose:
        return False
    if got_purpose.split(":", 1)[0] != str(purpose).split(":", 1)[0]:
        return False
    return hmac.compare_digest(sig, _sign(got_purpose, str(user_id), issued, nonce))


def verify_form_key(key: str, user_id: str, purpose: str) -> tuple:
    """`(True, "")` or `(False, reason)`. Signature, owner, purpose+SUBJECT and age.

    Binding the key to the user id is what stops a scraped key from being replayed by
    a different session; binding it to the purpose is what stops a key minted for
    "preview a repayment" from being spent as "confirm a bond redemption"; and binding
    it to the subject inside that purpose (`bid:41`, not `bid`) is what stops a key
    minted on the figures for one lot from committing a bid on another.
    """
    parts = str(key or "").rsplit(".", 3)
    if len(parts) != 4:
        return False, "This form is missing its confirmation key. Reload the page."
    got_purpose, issued, nonce, sig = parts
    if got_purpose != purpose:
        if is_subject_mismatch(key, user_id, purpose):
            noun = _SUBJECT_NOUN.get(str(purpose).split(":", 1)[0], "item")
            return False, (
                f"This confirmation belongs to a different {noun} than the one you just "
                f"submitted — the figures you were shown were for "
                f"{got_purpose.split(':', 1)[1]}, and this would have acted on "
                f"{str(purpose).split(':', 1)[1]}. Nothing was done. Reload the page and "
                f"confirm from that {noun}'s own figures.")
        return False, "This form's key is for a different action. Reload the page."
    if not hmac.compare_digest(sig, _sign(got_purpose, str(user_id), issued, nonce)):
        return False, "This form's key is not one we issued to you. Reload the page."
    try:
        age = time.time() - int(issued)
    except (TypeError, ValueError):
        return False, "This form's key is malformed. Reload the page."
    if age > FORM_KEY_TTL:
        return False, "This form expired. Reload the page and try again."
    if age < -60:
        return False, "This form's key is not valid yet. Reload the page."
    return True, ""


_IDEM_DDL = """
CREATE TABLE IF NOT EXISTS web_idempotency (
    key         TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'in_progress',   -- in_progress | done
    status      INTEGER,
    response    TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL
)
"""

_IDEM_READY = False


def _idem_db():
    import Restocker_db as _db
    return _db


def _ensure_idem_table() -> None:
    global _IDEM_READY
    if _IDEM_READY:
        return
    with _idem_db().db() as conn:
        conn.execute(_IDEM_DDL)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_web_idem_user "
                     "ON web_idempotency(user_id, endpoint)")
    _IDEM_READY = True


def claim_form_key(key: str, user_id: str, endpoint: str) -> tuple:
    """Claim-first. `("new", None)` | `("replay", (status, body))` | `("in_progress", None)`.

    The claim IS an `INSERT OR IGNORE` on a PRIMARY KEY, so of two concurrent submits of
    the same form exactly one gets `rowcount == 1` and proceeds; the loser reads the row
    back. That is the whole double-submit defence and it is one statement — a
    SELECT-then-INSERT here would let both copies through under exactly the conditions
    (a double-clicked button) that produce the duplicate in the first place.

    `in_progress` means the first copy has not answered yet. The caller must return
    "in flight, do not retry" rather than acting: the money call may be mid-flight, and
    a second one is the bug this key exists to prevent.
    """
    _ensure_idem_table()
    now = time.time()
    with _idem_db().db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO web_idempotency (key, user_id, endpoint, state, created_at) "
            "VALUES (?, ?, ?, 'in_progress', ?)",
            (str(key), str(user_id), str(endpoint), now),
        )
        if cur.rowcount == 1:
            return "new", None
        row = conn.execute(
            "SELECT user_id, endpoint, state, status, response FROM web_idempotency WHERE key=?",
            (str(key),),
        ).fetchone()
    if row is None:
        return "new", None
    if str(row["user_id"]) != str(user_id) or str(row["endpoint"]) != str(endpoint):
        # Signed keys make this near-impossible; if it ever happens it is a bug or an
        # attack, and either way the safe answer is to refuse, not to replay somebody
        # else's receipt at this session.
        log.warning("[vt_web][ATTACK-SIGNAL] idempotency key reused across user/endpoint: "
                    "key_owner=%s caller=%s key_endpoint=%s called=%s",
                    row["user_id"], user_id, row["endpoint"], endpoint)
        return "conflict", None
    if row["state"] != "done":
        return "in_progress", None
    try:
        body = json.loads(row["response"] or "{}")
    except Exception:
        body = {}
    return "replay", (int(row["status"] or 200), body)


def in_flight_keys(user_id: str, endpoint: str) -> list:
    """EVERY key this user still has `in_progress` on this endpoint, oldest first.

    THIS IS THE FIX FOR THE DOUBLE DEPOSIT. Downstream services (Osentar, and the bank
    side of every money POST here) dedupe on the key the BROWSER carried. So a render
    that mints a fresh key while an older one is still unresolved hands the player a
    key the downstream has never seen — and the downstream cannot dedupe a call it has
    never seen. The re-render must converge on the SAME key, not a new one.

    ALL of them, not the oldest one — that is WEB_VERIFY_R2 NEW-1 and it is the whole
    reason this returns a list. An endpoint whose purpose carries a subject has one key
    per subject (`bond_redeem:B-201`, `bond_redeem:B-777`), so with two redemptions
    stuck at the bank a single-row lookup re-issued the first bond's key and minted a
    FRESH one for the second — a key Osentar had never seen, on a bond Osentar may
    already have paid. Finding 2's double, one level down, per subject.

    A render path calling this must file each row under the subject its own purpose
    names (`form_key_subject`), re-issue that key for that subject alone, and say in
    words that the subject is awaiting the service. It must NOT quietly hand the key
    over as if it were fresh: submitting it returns 409 `in_flight`, and a player who
    was not told why deserves better than a 409.

    Each row is `{"key", "subject", "created_at", "updated_at", "note"}`. An endpoint
    with no subject yields at most one meaningful row and `subject` is `""`.
    """
    try:
        _ensure_idem_table()
        with _idem_db().db() as conn:
            rows = conn.execute(
                "SELECT key, created_at, updated_at, response FROM web_idempotency "
                "WHERE user_id=? AND endpoint=? AND state='in_progress' "
                "ORDER BY created_at ASC",
                (str(user_id), str(endpoint)),
            ).fetchall()
    except Exception as e:
        log.exception("[vt_web] in-flight key lookup failed for %s/%s: %s",
                      user_id, endpoint, e)
        return []
    out = []
    for row in rows or ():
        try:
            note = str((json.loads(row["response"] or "{}") or {}).get("unknown_outcome") or "")
        except Exception:
            note = ""
        out.append({"key": str(row["key"]),
                    "subject": form_key_subject(str(row["key"])),
                    "created_at": float(row["created_at"] or 0),
                    "updated_at": float(row["updated_at"] or 0),
                    "note": note})
    return out


def mark_key_unknown(key: str, reason: str) -> None:
    """Record WHY a claimed key is stuck, WITHOUT freeing it. Deliberately not a release.

    An unknown outcome — a timeout, a dropped connection, an exception between the two
    reads — must keep the key claimed. Freeing it is exactly how the double happens:
    the money call may have applied, and a freed key lets the retry apply it again.
    So this writes a reason and a timestamp and leaves `state='in_progress'` alone; the
    `WHERE state='in_progress'` clause means it can never overwrite a finished result.

    The row is now a work item for `reconcile_loop.py`, which is the only thing allowed
    to decide what actually happened at the far end.
    """
    try:
        _ensure_idem_table()
        with _idem_db().db() as conn:
            conn.execute(
                "UPDATE web_idempotency SET updated_at=?, response=? "
                "WHERE key=? AND state='in_progress'",
                (time.time(),
                 json.dumps({"unknown_outcome": str(reason)[:300]}, ensure_ascii=False),
                 str(key)),
            )
    except Exception:
        log.exception("[vt_web] could not mark key %s as unknown-outcome", key)


def finish_form_key(key: str, status: int, body: dict) -> None:
    """Record the answer so the replay returns THIS result and not a second action."""
    try:
        _ensure_idem_table()
        with _idem_db().db() as conn:
            conn.execute(
                "UPDATE web_idempotency SET state='done', status=?, response=?, updated_at=? "
                "WHERE key=?",
                (int(status), json.dumps(body, ensure_ascii=False), time.time(), str(key)),
            )
    except Exception as e:
        log.exception("[vt_web] could not record idempotent result for %s: %s", key, e)


def release_form_key(key: str) -> None:
    """Un-claim a key whose operation was refused BEFORE anything moved.

    Only ever called on a definite, no-effect refusal — "you are not the high bidder",
    "amount below the minimum". A timeout or an unknown outcome must NOT come here: the
    money call may have applied, and releasing the key would let the retry apply it
    again. Same rule as `bank_api._release_key`, and the same trap.
    """
    try:
        _ensure_idem_table()
        with _idem_db().db() as conn:
            conn.execute("DELETE FROM web_idempotency WHERE key=? AND state='in_progress'",
                         (str(key),))
    except Exception:
        pass


def replayed(body: dict) -> Any:
    """Return a stored result, marked so the page can say "already done" not "done"."""
    return web.json_response({**body, "replayed": True}, status=200)


# ══════════════════════════════════════════════════════════════════════════
# Money-form plumbing shared by both sections
# ══════════════════════════════════════════════════════════════════════════

async def money_post(request, endpoint: str, purpose, handler):
    """The wrapper every money POST in both sections goes through.

    `purpose` is either a plain string, for an action with no subject (a deposit is a
    deposit), or a CALLABLE taking the request body and returning the subject-bound
    purpose the render path minted — `lambda b: f"bid:{b['lot_id']}"`. It is called
    after the body is read and before anything is claimed, so the key is checked
    against the thing this request is actually about rather than against the endpoint
    it happened to be posted to (WEB_ATTACK finding 7).

    Order matters and every step is a refusal the section handler never has to write:

      1. session (401)  — logged-out POST refused before the body is read
      2. CSRF   (403)
      3. body-supplied identity noted as an attack signal and ignored
      4. form key verified — ours, this user's, this action's, THIS SUBJECT's,
         not stale (400)
      5. claimed single-use; a replay returns the ORIGINAL response (200, replayed:true)
      6. `handler(sess, body, key)` runs, and whatever it returns is recorded

    A handler that raises leaves the key claimed and `in_progress` on purpose: an
    exception mid-flight is exactly the case where we do not know whether the money
    moved, and the honest answer to the retry is "in flight", not a second attempt.
    A handler that wants the key released says so by raising `NoEffect`.
    """
    sess, refusal = require_post_session(request)
    if refusal is not None:
        return refusal
    body = await read_json(request)
    note_body_identity(request, body, sess)
    uid = str(sess["user_id"])

    key = str(body.get("idempotency_key") or body.get("key") or "").strip()
    try:
        want = purpose(body) if callable(purpose) else str(purpose)
    except Exception as e:
        # The subject could not be read out of the body at all, so there is no key that
        # could match it. Refuse before the claim rather than guessing a subject.
        log.warning("[vt_web] %s could not derive a form purpose: %s", endpoint, e)
        return json_err("bad_form_key",
                        "This form did not say what it was acting on. Reload the page.", 400)
    ok, why = verify_form_key(key, uid, want)
    if not ok:
        code = ("form_key_subject_mismatch" if is_subject_mismatch(key, uid, want)
                else "bad_form_key")
        return json_err(code, why, 400)

    state, stored = claim_form_key(key, uid, endpoint)
    if state == "replay":
        status, stored_body = stored
        return web.json_response({**stored_body, "replayed": True}, status=status)
    if state == "in_progress":
        return json_err("in_flight",
                        "That request is still being processed. Give it a moment — "
                        "do not submit again.", 409)
    if state == "conflict":
        return json_err("key_conflict", "That confirmation key is not yours.", 403)

    try:
        status, payload = await handler(sess, body, key)
    except NoEffect as e:
        release_form_key(key)
        return json_err(e.code, str(e), e.status)
    except Exception as e:
        # UNKNOWN OUTCOME. Explicit, not incidental: the key STAYS CLAIMED. We do not
        # know whether the money moved, and a key that is freed on an unknown outcome
        # is the double-deposit. We record the reason so the render path can say
        # "awaiting the service" instead of minting a second key, and so
        # `reconcile_loop.py` has something to look at once somebody wires it. Then the exception carries on to the route's own
        # handler (OsentarDown -> 503, ValueError -> 400) exactly as before.
        mark_key_unknown(key, f"{type(e).__name__}: {e}")
        log.warning("[vt_web] %s left key %s claimed on an unknown outcome: %s",
                    endpoint, key[:20], e)
        raise
    finish_form_key(key, status, payload)
    return web.json_response(payload, status=status)


class NoEffect(Exception):
    """A definite refusal that provably moved nothing — the key may be released.

    Raise this ONLY when the operation was rejected before any call to a money service,
    or by a money service that answered a definite `insufficient`/`frozen`-class refusal.
    Never for a timeout, never for an unknown outcome.
    """

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# ══════════════════════════════════════════════════════════════════════════
# Page rendering
# ══════════════════════════════════════════════════════════════════════════

#: The nav. Betting is NOT here and must not be added: the owner scrapped the casino.
#: Prediction markets live under Estates because they are pari-mutuel — players stake
#: against each other and the house takes a rake, never a side.
NAV = (
    ("hub", "Hub", "/hub"),
    ("markets", "Markets", "/exchange"),
    ("banking", "Banking", "/banking"),
    ("estates", "Lands · Auctions · Predictions", "/estates"),
    ("messages", "Messages", "/messages"),
)



#: Icon set — inline SVG, lucide-style, 14px, stroke 1.7. THEME.md rule 7: no emoji,
#: anywhere, ever. Rendered into a JS object so page scripts can compose with it.
_ICONS_JS = r"""
const svg = d => '<svg class="i" viewBox="0 0 24 24">' + d + '</svg>';
const IC = {
  hub:     svg('<path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"/>'),
  markets: svg('<path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/>'),
  banking: svg('<path d="M3 21h18M4 10h16M12 3l9 5H3zM6 10v11M10 10v11M14 10v11M18 10v11"/>'),
  estates: svg('<path d="M9 4L3 7v13l6-3 6 3 6-3V4l-6 3z"/><path d="M9 4v13M15 7v13"/>'),
  messages: svg('<path d="M4 4h16v12H8l-4 4z"/><path d="M8 9h8M8 12h5"/>'),
  arrow:   svg('<path d="M5 12h14M13 6l6 6-6 6"/>'),
  back:    svg('<path d="M19 12H5M11 18l-6-6 6-6"/>'),
  check:   svg('<path d="M20 6L9 17l-5-5"/>'),
  cross:   svg('<path d="M18 6L6 18M6 6l12 12"/>'),
  lock:    svg('<rect x="4" y="11" width="16" height="10"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>'),
  alert:   svg('<path d="M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'),
  clock:   svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>')
};
"""

#: Formatting, the shared flow engine, and the money strip. Everything a page needs
#: that is not that page's own content.
_BASE_JS = r"""
/* ---------- format: integer coins, human dates, never ISO on screen ---------- */
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const n  = v => Math.round(Number(v) || 0).toLocaleString('en-US');
const cn = v => n(v) + '<span class="coin">c</span>';
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmtD(s){
  if(!s) return '—';
  const d = new Date(String(s).length <= 10 ? s + 'T00:00:00Z' : s);
  if(isNaN(d)) return '—';
  return d.getUTCDate() + ' ' + MON[d.getUTCMonth()] + ' ' + d.getUTCFullYear();
}
function rel(s){
  if(!s) return '';
  const d = new Date(String(s).length <= 10 ? s + 'T00:00:00Z' : s);
  if(isNaN(d)) return '';
  const k = Math.round((d - new Date()) / 86400000);
  if(k === 0) return 'today';
  if(k === 1) return 'tomorrow';
  if(k === -1) return 'yesterday';
  const a = Math.abs(k);
  const unit = a < 14 ? a + ' days' : a < 60 ? Math.round(a/7) + ' weeks'
                                             : Math.round(a/30) + ' months';
  return k > 0 ? 'in ' + unit : unit + ' ago';
}
function fmtLeft(secs){
  secs = Math.max(0, Math.floor(secs || 0));
  if(secs <= 0) return 'closed';
  const d = Math.floor(secs/86400), h = Math.floor(secs%86400/3600),
        m = Math.floor(secs%3600/60), s = secs%60;
  if(d) return d + 'd ' + (h ? String(h).padStart(2,'0') + 'h' : String(m).padStart(2,'0') + 'm');
  if(h) return h + 'h ' + String(m).padStart(2,'0') + 'm';
  if(m) return String(m).padStart(2,'0') + 'm ' + String(s).padStart(2,'0') + 's';
  return s + 's';
}

/* ---------- session ---------- */
const VT = { me:null, csrf:'' };
async function loadMe(){
  try{
    const r = await fetch('/api/me', {credentials:'same-origin'});
    const j = await r.json();
    VT.me = (j && j.logged_in) ? j : null;
    VT.csrf = (j && j.csrf) || '';
  }catch(e){ VT.me = null; }
  const tag = document.getElementById('authTag');
  if(tag){
    tag.innerHTML = VT.me
      ? '<span class="user-mark">' + esc((VT.me.name||'?').slice(0,2).toUpperCase()) + '</span>' +
        '<span class="auth-name">@' + esc(VT.me.name || 'linked') + '</span>'
      : '<button class="btn ghost" onclick="doLink()">Log in</button>';
  }
  return VT.me;
}
async function doLink(){
  const code = (window.prompt('Run /website_login in Discord, then paste your code here:') || '').trim();
  if(!code) return;
  try{
    const r = await fetch('/api/link', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({code})});
    const j = await r.json();
    if(j && j.ok) location.reload(); else alert((j && j.error) || 'Login failed.');
  }catch(e){ alert('Login failed.'); }
}
/* Every POST on this site goes through here: same-origin cookie, CSRF header, JSON.
   No handler builds its own fetch, so no handler can forget the token. */
async function post(url, body){
  const r = await fetch(url, {method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json', 'X-CSRF-Token': VT.csrf},
    body: JSON.stringify(body || {})});
  let j = {};
  try{ j = await r.json(); }catch(e){ j = {ok:false, code:'bad_response', error:'The server did not answer in JSON.'}; }
  j._status = r.status;
  return j;
}
async function get(url){
  const r = await fetch(url, {credentials:'same-origin'});
  let j = {};
  try{ j = await r.json(); }catch(e){ j = {ok:false, code:'bad_response', error:'The server did not answer in JSON.'}; }
  j._status = r.status;
  return j;
}

/* ---------- money strip ---------- */
async function renderStrip(){
  const j = await get('/api/wallet/strip');
  const el = id => document.getElementById(id);
  if(!j.ok){
    if(el('sAvail')) el('sAvail').textContent = '—';
    if(el('sHeld'))  el('sHeld').textContent  = '—';
    if(el('sSave'))  el('sSave').textContent  = '—';
    if(el('sNet'))   el('sNet').textContent   = '—';
    if(el('sHeldSub')) el('sHeldSub').textContent = j.error || 'wallet unavailable';
    return;
  }
  el('sAvail').innerHTML = cn(j.available);
  el('sHeld').innerHTML  = cn(j.held);
  el('sSave').innerHTML  = j.savings === null ? '<span class="muted">unavailable</span>' : cn(j.savings);
  el('sSaveSub').textContent = j.savings === null
    ? (j.bank_error || 'Osentar Bank unreachable')
    : (j.savings_apr != null ? j.savings_apr + '% APR' : 'Osentar Bank');
  /* Net is withheld, not guessed, when a term of it is missing. A net position that
     silently drops savings and debt is a wrong number wearing a confident font. */
  el('sNet').innerHTML = j.net === null ? '<span class="muted">—</span>' : cn(j.net);
  const note = document.getElementById('netNote');
  if(note) note.textContent = j.net === null
    ? 'withheld — Osentar Bank is unreachable, so savings and loan debt are unknown'
    : 'available + held + savings − loan · bonds & land excluded';
  el('sHeldSub').textContent = j.holds.length
    ? 'reserved by ' + j.holds.length + ' thing' + (j.holds.length > 1 ? 's' : '')
    : 'nothing reserved';
  el('sHeldTotal').innerHTML = cn(j.held);
  const list = document.getElementById('holdList');
  list.innerHTML = j.holds.length ? j.holds.map(h => `
      <div class="hold-row">
        <span class="hold-dot" style="background:${esc(h.color || 'var(--amber)')}"></span>
        <span class="hold-main">
          <span class="hold-t">${esc(h.title)}</span>
          <span class="hold-s">${esc(h.sub)}</span>
        </span>
        <span class="hold-amt num">${cn(h.amount)}</span>
      </div>`).join('')
    : '<div class="empty">No open holds. All of your coins are available.</div>';
  /* The rows above are meant to BE the held figure. If they are not, say so — a
     drawer that quietly lists 5,000c under a heading reading 12,000c is a lie the
     player pays for in a support ticket. */
  const gap = document.getElementById('holdGap');
  if(gap){
    const u = Number(j.unaccounted || 0);
    if(u){
      gap.classList.remove('hide');
      gap.innerHTML = u > 0
        ? 'These rows account for <b class="num">' + cn(j.holds_sum) + '</b> of the '
          + '<b class="num">' + cn(j.held) + '</b> held. The rest is reserved by '
          + 'something this page could not read just now — your coins are not lost. '
          + 'Reload, and tell staff if it stays.'
        : 'These rows add up to more than the held figure above. Something is out of '
          + 'step — reload, and tell staff if it stays.';
    } else {
      gap.classList.add('hide');
      gap.innerHTML = '';
    }
  }
}
document.addEventListener('DOMContentLoaded', () => {
  const seg = document.getElementById('heldSeg');
  if(seg) seg.onclick = () => {
    const open = document.getElementById('stripWrap').classList.toggle('open');
    seg.setAttribute('aria-expanded', open);
  };
});

/* ================================================================
   FLOW ENGINE — every money action is preview -> confirm -> receipt.
   The preview is computed BY THE SERVER and the figures shown are the
   figures the server is about to move. Nothing moves until Confirm,
   and the server re-checks everything at Confirm regardless of what
   this screen showed.
   ================================================================ */
let F = null;
function closeModal(){ const s = document.getElementById('scrim'); if(s) s.classList.remove('on'); F = null; }
async function openFlow(cfg){
  F = Object.assign({step:1, amount:''}, cfg);
  document.getElementById('scrim').classList.add('on');
  if(!F.amountStep){
    /* A flow with no amount step opens ON the preview, so the preview has to be
       fetched before the first render — otherwise the confirm screen paints before
       the figures it is confirming exist, which is the one thing it must never do. */
    document.getElementById('mTitle').textContent = F.title;
    document.getElementById('mSub').textContent = F.sub || '';
    document.getElementById('mBody').innerHTML = '<div class="empty">Checking…</div>';
    document.getElementById('mFoot').innerHTML = '';
    F.pv = await F.preview(amtInt());
    if(!F) return;                       /* closed while we were waiting */
  }
  renderFlow();
}
function amtInt(){ return parseInt(F.amount || '0', 10) || 0; }
function setAmt(v){ F.amount = String(v); const el = document.getElementById('amt'); if(el) el.value = n(v); validate(); }
function onAmtInput(el){
  const raw = el.value.replace(/[^\d]/g, '');
  F.amount = raw; el.value = raw ? n(parseInt(raw, 10)) : '';
  validate();
}
function validate(){
  const e = document.getElementById('err'), b = document.getElementById('go1');
  if(!e || !b) return;
  const msg = F.check ? F.check(amtInt()) : '';
  e.textContent = msg || '';
  b.disabled = !!msg || amtInt() <= 0;
}
function renderFlow(){
  const steps = F.amountStep ? 3 : 2;
  const at = F.amountStep ? F.step : F.step + 1;
  document.getElementById('dots').innerHTML = Array.from({length:steps}, (_, k) => {
    const i = F.amountStep ? k+1 : k+2;
    return '<i class="sd ' + (at > i ? 'done' : (at === i ? 'on' : '')) + '"></i>';
  }).join('');
  document.getElementById('mTitle').textContent = F.step === 3 ? (F.doneTitle || F.title) : F.title;
  document.getElementById('mSub').textContent   = F.step === 3 ? (F.doneSub || F.sub) : F.sub;
  if(F.step === 1 && F.amountStep) return stepAmount();
  if(F.step === 3) return stepReceipt();
  return stepPreview();
}
function stepAmount(){
  document.getElementById('mBody').innerHTML = `
    <div class="between" style="margin-bottom:12px">
      <span class="tile-h" style="margin:0">${esc(F.amountLabel)}</span>
      <span class="num" style="font-weight:600">${cn(F.amountCap)}</span>
    </div>
    <div class="amount-wrap">
      <span class="cn">c</span>
      <input id="amt" class="amount num" inputmode="numeric" placeholder="0" autocomplete="off"
             value="${F.amount ? n(amtInt()) : ''}" oninput="onAmtInput(this)">
    </div>
    <div class="chips">${(F.chips || []).map(c =>
      `<button class="chip" onclick="setAmt(${c[1]})">${esc(c[0])}</button>`).join('')}</div>
    <div class="err" id="err"></div>
    ${F.amountRows ? F.amountRows() : ''}`;
  document.getElementById('mFoot').innerHTML =
    `<button class="btn ghost" onclick="closeModal()">Cancel</button>
     <button class="btn" id="go1" disabled onclick="goPreview()">Preview ${IC.arrow}</button>`;
  validate();
  setTimeout(() => { const a = document.getElementById('amt'); if(a) a.focus(); }, 30);
}
async function goPreview(){
  const b = document.getElementById('go1');
  if(b){ b.disabled = true; b.textContent = 'Checking…'; }
  F.pv = await F.preview(amtInt());
  F.step = 2;
  renderFlow();
}
function stepPreview(){
  const p = F.pv || {};
  const body = document.getElementById('mBody');
  if(!p.ok){
    body.innerHTML = `<div class="notebox">${esc(p.error || 'The server would not price this.')}</div>`;
    document.getElementById('mFoot').innerHTML = F.amountStep
      ? `<button class="btn ghost" onclick="F.step=1;renderFlow()">${IC.back} Change amount</button>`
      : `<button class="btn ghost" onclick="closeModal()">Close</button>`;
    return;
  }
  body.innerHTML = `
    <div class="tile-h" style="margin-bottom:8px">${esc(p.head || 'Preview — nothing has moved yet')}</div>
    ${(p.rows || []).map(r =>
      `<div class="kv"><span class="k">${esc(r[0])}</span><span class="v ${esc(r[2] || '')}">${r[1]}</span></div>`).join('')}
    ${p.total ? `<div class="kv total"><span class="k">${esc(p.total[0])}</span>
      <span class="v num" style="${esc(p.total[2] || '')}">${p.total[1]}</span></div>` : ''}
    ${p.effect ? `<div style="margin-top:16px;padding:12px;border:1px solid var(--border);background:var(--panel2)">
      ${p.effect.map(r => `<div class="kv" style="border:none;padding:4px 0"><span class="k">${esc(r[0])}</span>
        <span class="v num" style="${esc(r[2] || '')}">${r[1]}</span></div>`).join('')}</div>` : ''}
    ${p.note ? `<div class="notebox">${p.note}</div>` : ''}`;
  const back = F.amountStep
    ? `<button class="btn ghost" onclick="F.step=1;renderFlow()">${IC.back} Change amount</button>`
    : `<button class="btn ghost" onclick="closeModal()">Cancel</button>`;
  document.getElementById('mFoot').innerHTML = back + (p.blocked
    ? `<button class="btn" disabled>${esc(p.confirm_label || 'Confirm')}</button>`
    : `<button class="btn ${p.danger ? 'danger' : ''}" id="goC" onclick="doConfirm()">${esc(p.confirm_label || 'Confirm')}</button>`);
}
async function doConfirm(){
  const b = document.getElementById('goC');
  if(b){ b.disabled = true; b.textContent = 'Working…'; }
  F.receipt = await F.commit(amtInt());
  F.step = 3;
  renderFlow();
  renderStrip();
}
function stepReceipt(){
  const r = F.receipt || {};
  const bad = !r.ok;
  document.getElementById('mBody').innerHTML = `
    <div class="big-ok" style="${bad ? 'border-color:rgba(255,77,77,.35);background:rgba(255,77,77,.08);color:var(--red)' : ''}">${bad ? IC.cross : IC.check}</div>
    <div style="text-align:center;margin-bottom:16px">
      <div class="num big">${r.big || (bad ? 'Not done' : 'Done')}</div>
      <div class="sec" style="font-size:12px">${esc(r.big_sub || '')}</div>
    </div>
    ${(r.rows || []).map(x => `<div class="kv"><span class="k">${esc(x[0])}</span>
      <span class="v ${esc(x[2] || '')}">${x[1]}</span></div>`).join('')}
    <div class="${bad ? 'notebox' : 'okbox'}">${r.note || esc(r.error || '')}</div>`;
  document.getElementById('mFoot').innerHTML =
    `<button class="btn" onclick="closeModal();location.reload()">Done</button>`;
}
document.addEventListener('DOMContentLoaded', () => {
  const s = document.getElementById('scrim');
  if(s) s.addEventListener('click', e => { if(e.target === s) closeModal(); });
  document.addEventListener('keydown', e => { if(e.key === 'Escape' && F) closeModal(); });
});

/* ---------- unread messages: the nav badge, on every page of the site ----------
   Lives here rather than in `messages_web`'s page script because a count only shown
   on the Messages page tells you nothing you did not already know by being there.
   Fails silent and hidden: logged out, section not mounted, endpoint 404 — all of
   them mean "no badge", never a broken nav. */
async function refreshUnread(){
  const el = document.getElementById('navUnread');
  if(!el) return;
  try{
    const r = await fetch('/api/messages/unread', {credentials:'same-origin'});
    const j = await r.json();
    if(j && j.ok && Number(j.unread) > 0){
      el.textContent = n(j.unread);
      el.style.display = 'inline-flex';
      return;
    }
  }catch(e){ /* fall through to hidden */ }
  el.style.display = 'none';
}
refreshUnread();
setInterval(refreshUnread, 60000);
"""


def _nav_html(active: str) -> str:
    out = []
    for key, label, href in NAV:
        cur = ' aria-current="true"' if key == active else ""
        # The unread badge lives in the nav rather than the money strip: the strip is
        # coins and only coins, and a count with no unit standing in that row is the
        # bug UI_PRINCIPLES part 3 opens with. It starts hidden and is filled by
        # `refreshUnread()` below; a logged-out visitor, or a deploy without the
        # messages section mounted, simply never shows it.
        badge = ('<span class="nav-badge" id="navUnread" style="display:none"></span>'
                 if key == "messages" else "")
        out.append(f'<a class="nav-tab" href="{href}" data-k="{key}"{cur}>'
                   f'<span class="nav-ic" data-ic="{key}"></span>{html.escape(label)}'
                   f'{badge}</a>')
    return "".join(out)


_STRIP_HTML = """
<div class="strip-wrap" id="stripWrap">
  <div class="strip">
    <div class="seg">
      <div class="lab">Wallet · available</div>
      <div class="val v-avail num" id="sAvail">—</div>
      <div class="sub">spendable right now</div>
    </div>
    <button class="seg held-seg" id="heldSeg" aria-expanded="false">
      <div class="lab">Held <span class="chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg></span></div>
      <div class="val v-held num" id="sHeld">—</div>
      <div class="sub" id="sHeldSub">&nbsp;</div>
    </button>
    <div class="seg">
      <div class="lab">Savings · Osentar</div>
      <div class="val v-save num" id="sSave">—</div>
      <div class="sub" id="sSaveSub">&nbsp;</div>
    </div>
    <div class="spacer"></div>
    <div class="seg net-seg">
      <div class="lab">Net position</div>
      <div class="val v-net num" id="sNet">—</div>
      <div class="net-note" id="netNote">available + held + savings &minus; loan · bonds &amp; land excluded</div>
    </div>
  </div>
  <div class="drawer">
    <div class="drawer-inner">
      <div class="drawer-head">What is holding your coins</div>
      <div id="holdList"></div>
      <!-- The drawer's honesty line. Hidden while the rows account for the printed
           total; visible, in words, the moment they do not. -->
      <div class="holdgap hide" id="holdGap"></div>
      <div class="drawer-foot">
        <span>Holds are escrow — the coins are yours until captured, and every hold has an expiry.</span>
        <span>Total held <b id="sHeldTotal">—</b></span>
      </div>
    </div>
  </div>
</div>
"""

_MODAL_HTML = """
<div class="scrim" id="scrim">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <div><h3 id="mTitle">Confirm</h3><div class="mh-sub" id="mSub"></div></div>
      <div class="row" style="gap:14px">
        <div class="step-dots" id="dots"></div>
        <button class="x" onclick="closeModal()" aria-label="Close"><svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
      </div>
    </div>
    <div class="modal-body" id="mBody"></div>
    <div class="modal-foot" id="mFoot"></div>
  </div>
</div>
"""


def page(title: str, nav_key: str, body: str, page_js: str = "", strip: bool = True) -> Any:
    """Render a full page in the house style. Returns an aiohttp `web.Response`.

    Same templating convention as the rest of `Restocker_web`: a Python string, no
    Jinja, no template directory, no static files — the app registers no static route
    and adding one for two pages would be a deployment change nobody asked for.
    """
    doc = _PAGE.replace("__TITLE__", html.escape(title))
    doc = doc.replace("__NAV__", _nav_html(nav_key))
    doc = doc.replace("__STRIP__", _STRIP_HTML if strip else "")
    doc = doc.replace("__MODAL__", _MODAL_HTML)
    doc = doc.replace("__ICONS__", _ICONS_JS)
    doc = doc.replace("__BASEJS__", _BASE_JS)
    doc = doc.replace("__BODY__", body)
    doc = doc.replace("__PAGEJS__", page_js)
    return web.Response(text=doc, content_type="text/html", charset="utf-8")


_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · V Tech</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --font-data:'IBM Plex Mono',ui-monospace,Menlo,monospace;
  --font-ui:'Space Grotesk',system-ui,sans-serif;
  --bg:#080808;--surface:#0f0f0f;--panel2:#151515;--border:#1E1E1E;--border-strong:#2A2A2A;
  --text:#F4F4F4;--text-body:#B4B4B4;--muted:#6a6a6a;--faint:#3f3f3f;
  --green:#22FF7A;--green-dim:#0f7a3a;--accent:#22FF7A;--red:#FF4D4D;--amber:#F5A623;--blue:#4A9EFF;--purple:#B47FFF;--nether:#FF6B35;
}
:root{
  /* money roles map onto the house palette */
  --money-available:var(--text);
  --money-held:var(--amber);
  --money-debt:var(--red);
  --money-save:var(--green);
  --headh:54px;--navh:46px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark}
body{background:var(--bg);background-image:radial-gradient(#151515 .5px,transparent .5px);background-size:24px 24px;color:var(--text);font-family:var(--font-ui);font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased;min-height:100vh}
.mono,td,.num,input,.pill,.tag,.val,.fv,.tick,.o-odds,.lot-bid,.hold-amt,.kfig,.big{font-family:var(--font-data);font-variant-numeric:tabular-nums slashed-zero}
a{color:inherit;text-decoration:none}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
::selection{background:rgba(34,255,122,.22)}
svg.i{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;flex:0 0 auto}

/* ---------- header ---------- */
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

/* ---------- nav ---------- */
nav{position:sticky;top:var(--headh);z-index:80;display:flex;gap:2px;background:var(--surface);border-bottom:1px solid var(--border);padding:0 16px;overflow-x:auto;height:var(--navh);scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
.nav-tab{display:flex;align-items:center;gap:7px;height:var(--navh);padding:0 16px;font-size:12px;font-weight:500;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;user-select:none;background:none}
.nav-tab svg{opacity:.7}
.nav-tab:hover{color:var(--text)}
.nav-tab[aria-current="true"]{color:var(--accent);border-bottom-color:var(--accent)}
.nav-tab[aria-current="true"] svg{opacity:1}

/* ---------- MONEY STRIP ---------- */
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

/* held drawer */
.drawer{max-height:0;overflow:hidden;transition:max-height .22s ease;border-top:1px solid transparent}
.strip-wrap.open .drawer{max-height:400px;border-top-color:var(--border)}
.drawer-inner{max-width:1180px;margin:0 auto;padding:12px 24px 16px}
.drawer-head{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600;margin-bottom:9px;display:flex;align-items:center;gap:10px}
.drawer-head::after{content:"";flex:1;height:1px;background:var(--border)}
.hold-row{display:flex;align-items:center;gap:12px;width:100%;text-align:left;padding:9px 12px;border:1px solid var(--border);background:var(--panel2);margin-bottom:6px;transition:border-color .12s,transform .12s;cursor:pointer}
.hold-row:hover{border-color:var(--border-strong);transform:translateX(2px)}
.hold-dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto;display:inline-block}
.hold-main{flex:1;min-width:0}
.hold-t{font-size:12.5px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.hold-s{font-size:10.5px;color:var(--muted);display:block}
.hold-amt{font-size:14px;font-weight:600;color:var(--money-held);white-space:nowrap}
.hold-go{color:var(--faint);display:flex}
.drawer-foot{display:flex;justify-content:space-between;align-items:center;gap:16px;padding-top:9px;margin-top:6px;border-top:1px solid var(--border);font-size:11px;color:var(--muted)}
.holdgap{font-size:11px;line-height:1.55;color:var(--muted);padding:9px 0 0;margin-top:6px;border-top:1px solid var(--border)}
.holdgap b{color:var(--money-held);font-family:var(--font-data);font-variant-numeric:tabular-nums slashed-zero}
.drawer-foot b{color:var(--money-held);font-size:13px;font-family:var(--font-data);font-variant-numeric:tabular-nums slashed-zero}

/* ---------- layout ---------- */
main{max-width:1180px;margin:0 auto;padding:24px 24px 80px;animation:f .25s ease}
@keyframes f{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.page-head{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:21px;font-weight:600;letter-spacing:-.01em;margin:0}
.page-sub{color:var(--muted);font-size:12.5px;margin-top:3px}
.section-h{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:24px 0 12px;display:flex;align-items:center;gap:10px;font-weight:600}
.section-h::after{content:"";flex:1;height:1px;background:var(--border)}
.tile-h{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;gap:10px;font-weight:600}
.eyebrow{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);font-weight:600;margin-bottom:4px}
.q{font-size:14px;font-weight:600;letter-spacing:-.01em;color:var(--text)}
.t-title{font-size:13.5px;font-weight:600;letter-spacing:-.01em}

.bento{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--border);padding:18px 20px;position:relative}
.s12{grid-column:span 12}.s7{grid-column:span 7}.s6{grid-column:span 6}
.s5{grid-column:span 5}.s4{grid-column:span 4}.s3{grid-column:span 3}
@media(max-width:900px){.s7,.s6,.s5,.s4,.s3{grid-column:span 12}}

/* hub cards */
.hubcard{border-top:2px solid var(--accent-c);cursor:pointer;display:flex;flex-direction:column;gap:14px;transition:border-color .12s,background .12s}
.hubcard:hover{background:var(--panel2);border-left-color:var(--border-strong);border-right-color:var(--border-strong);border-bottom-color:var(--border-strong)}
.hubcard .hc-name{display:flex;align-items:center;gap:8px;font-size:13.5px;font-weight:600;letter-spacing:-.01em}
.hubcard .desc{color:var(--muted);font-size:11.5px;margin-top:4px}
.figs{display:flex;border-top:1px solid var(--border);padding-top:12px;margin-top:auto}
.fig{flex:1;padding-right:10px;min-width:0}
.fig .fv{font-size:17px;font-weight:600;letter-spacing:-.01em}
.fig .fl{font-size:9.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-top:2px}
.hubcard .go{font-size:11px;color:var(--text-body);display:flex;align-items:center;gap:6px;text-transform:uppercase;letter-spacing:.06em;font-weight:500}
.badge-dot{width:8px;height:8px;background:var(--accent-c);display:inline-block}

/* kpi */
.kgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.kpi{background:var(--panel2);border:1px solid var(--border);padding:13px 14px}
.kpi .k{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}
.kpi .kfig{font-size:20px;font-weight:600;margin-top:5px}
.kpi .kt{font-size:10px;color:var(--muted);margin-top:3px}

/* generic bits */
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
.right{text-align:right}

/* tables */
table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border)}
th,td{padding:9px 13px;text-align:right;border-bottom:1px solid var(--border);font-size:12.5px}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600;font-family:var(--font-ui);font-size:11px;text-transform:uppercase;letter-spacing:.05em;cursor:pointer;user-select:none}
th:hover{color:var(--text)}
tbody tr:hover{background:var(--panel2)}
tr:last-child td{border-bottom:none}
.tick{font-weight:600;letter-spacing:.02em}
.tname{font-size:10.5px;color:var(--muted);font-family:var(--font-ui)}
.tablewrap{overflow-x:auto}

/* bars */
.bar{height:6px;background:var(--panel2);overflow:hidden;position:relative}
.bar > i{display:block;height:100%;background:linear-gradient(90deg,var(--green),#17b558)}

/* outcomes */
.outcome{display:grid;grid-template-columns:1fr auto;gap:4px 14px;align-items:center;padding:10px 12px;border:1px solid var(--border);background:var(--panel2);margin-bottom:8px;transition:border-color .12s}
.outcome:hover{border-color:var(--border-strong)}
.outcome.mine{border-color:rgba(74,158,255,.4)}
.o-name{font-size:12.5px;font-weight:500;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.o-right{text-align:right;font-size:12px;white-space:nowrap}
.o-odds{font-weight:600}
.o-barwrap{grid-column:1/-1;display:flex;align-items:center;gap:10px;margin-top:4px}
.o-barwrap .bar{flex:1}
.o-pct{font-size:10.5px;color:var(--muted);width:38px;text-align:right;font-family:var(--font-data);font-variant-numeric:tabular-nums slashed-zero}

/* auction lots */
.lot{border:1px solid var(--border);border-top:2px solid var(--lc);background:var(--surface);display:flex;flex-direction:column}
.lot:hover{border-left-color:var(--border-strong);border-right-color:var(--border-strong);border-bottom-color:var(--border-strong)}
.lot-body{padding:16px;display:flex;flex-direction:column;gap:10px;flex:1}
.lot-id{font-family:var(--font-data);font-size:9.5px;text-transform:uppercase;letter-spacing:.14em;color:var(--muted)}
.lot-t{font-size:13px;font-weight:600;letter-spacing:-.01em;margin-top:5px}
.lot-m{font-size:10.5px;color:var(--muted)}
.lot-bid{font-size:19px;font-weight:600;letter-spacing:-.01em}

/* chart */
.chart-legend{display:flex;gap:16px;font-size:10.5px;color:var(--muted);align-items:center;text-transform:uppercase;letter-spacing:.08em}
.lg{display:flex;align-items:center;gap:6px}
.lg i{width:12px;height:2px;display:block}

/* modal */
.scrim{position:fixed;inset:0;background:rgba(4,4,4,.82);z-index:200;display:none;align-items:center;justify-content:center;padding:20px}
.scrim.on{display:flex}
.modal{width:100%;max-width:470px;background:var(--surface);border:1px solid var(--border-strong);max-height:92vh;display:flex;flex-direction:column;animation:f .18s ease}
.modal-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.modal-head h3{margin:0;font-size:13px;font-weight:600;letter-spacing:-.01em}
.modal-head .mh-sub{font-size:10.5px;color:var(--muted);margin-top:3px}
.modal-body{padding:18px;overflow-y:auto}
.modal-foot{padding:13px 18px;border-top:1px solid var(--border);display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap}
.x{color:var(--muted);display:flex;padding:2px}
.x:hover{color:var(--text)}
.amount-wrap{position:relative;margin-bottom:10px}
.amount{width:100%;padding:13px 14px 13px 40px;font-size:22px;font-weight:600;background:var(--bg);border:1px solid var(--border);color:var(--text);outline:none;letter-spacing:-.01em}
.amount:focus{border-color:var(--accent)}
.amount-wrap .cn{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:15px;font-family:var(--font-data)}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:14px}
.chip{padding:5px 10px;border:1px solid var(--border);background:var(--panel2);font-size:11px;font-weight:500;color:var(--text-body);font-family:var(--font-data);font-variant-numeric:tabular-nums slashed-zero}
.chip:hover{border-color:var(--border-strong);color:var(--text)}
.err{color:var(--red);font-size:11.5px;min-height:18px}
.kv{display:flex;justify-content:space-between;gap:16px;padding:8px 0;font-size:12.5px;border-bottom:1px solid var(--border)}
.kv:last-of-type{border-bottom:none}
.kv .k{color:var(--muted)}
.kv .v{font-weight:600;white-space:nowrap;text-align:right}
.kv.total{border-top:1px solid var(--border);border-bottom:none;margin-top:6px;padding-top:12px;font-size:13.5px}
.notebox{margin-top:14px;padding:10px 12px;font-size:11px;line-height:1.55;background:rgba(245,166,35,.06);border:1px solid rgba(245,166,35,.25);color:#d9ae6a}
.notebox.blue{background:rgba(74,158,255,.06);border-color:rgba(74,158,255,.25);color:#9dc3f0}
.okbox{margin-top:14px;padding:11px 12px;font-size:11.5px;line-height:1.55;background:rgba(34,255,122,.06);border:1px solid rgba(34,255,122,.25);color:#8fe0b2}
.big-ok{width:40px;height:40px;background:rgba(34,255,122,.08);border:1px solid rgba(34,255,122,.35);display:flex;align-items:center;justify-content:center;margin:0 auto 14px;color:var(--green)}
.big-ok svg{width:20px;height:20px}
.step-dots{display:flex;gap:6px;align-items:center}
.sd{width:6px;height:6px;border-radius:50%;background:var(--border-strong);display:inline-block}
.sd.on{background:var(--accent)}
.sd.done{background:var(--green-dim)}
.big{font-size:24px;font-weight:600;letter-spacing:-.02em}
.ref{font-family:var(--font-data);font-size:11px;color:var(--text-body)}
.flash{animation:flash 1.4s ease-out}
@keyframes flash{0%{background:rgba(245,166,35,.22)}100%{background:transparent}}
.empty{padding:16px 0;color:var(--muted);font-size:12px}
.foot{color:var(--faint);font-size:11px;margin-top:12px;line-height:1.5}
.hide{display:none !important}

/* responsive */
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
  .kgrid{grid-template-columns:1fr}
}
.nav-ic{display:inline-flex;align-items:center}
.nav-badge{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:16px;margin-left:6px;padding:0 4px;background:var(--accent);color:#000;font-family:var(--font-data);font-size:10px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1}
.bank-down{border:1px solid rgba(245,166,35,.35);background:rgba(245,166,35,.07);padding:14px 16px;margin-bottom:14px}
.bank-down .bd-h{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--amber);font-weight:600;margin-bottom:6px}
.bank-down .bd-b{font-size:12.5px;color:var(--text-body);line-height:1.55}
.bank-down .bd-b code{font-family:var(--font-data);font-size:11.5px;color:var(--muted)}
.holdnote{font-size:11.5px;color:var(--muted);line-height:1.5;margin-top:6px}
.holdnote b{color:var(--amber);font-weight:600}
</style>
</head><body>
<header>
  <a class="logo" href="/hub">
    <div class="logo-icon">V</div>
    <div><div class="logo-text">V Tech Hub</div><div class="logo-sub">One economy</div></div>
  </a>
  <div class="header-right">
    <span class="svc-note">Markets · Banking · Estates · Messages</span>
    <span class="user-tag" id="authTag"></span>
  </div>
</header>
<nav id="tabs">__NAV__</nav>
__STRIP__
<main id="view">__BODY__</main>
__MODAL__
<script>
__ICONS__
document.querySelectorAll('.nav-ic').forEach(e => { e.innerHTML = IC[e.dataset.ic] || ''; });
__BASEJS__
__PAGEJS__
</script>
</body></html>
"""


# ══════════════════════════════════════════════════════════════════════════
# The one route this module owns
# ══════════════════════════════════════════════════════════════════════════

def _hold_colour(reason: str) -> str:
    r = str(reason or "")
    if r.startswith("realestate:") or r.startswith("land:"):
        return "var(--nether)"
    if r.startswith("estates:market:") or r.startswith("bet:"):
        return "var(--purple)"
    return "var(--amber)"


async def _handle_strip(request):
    """`{available, held, holds[], savings, net}` for the strip on every page.

    Available and held are read from `ledger_v2` in-process and are always real.
    Savings and loan debt come from Osentar over HTTP and may be absent — in which case
    they are `null` and so is `net`, with `bank_error` naming why.
    """
    sess, refusal = require_session(request)
    if refusal is not None:
        return refusal
    uid = str(sess["user_id"])

    try:
        import ledger_v2 as _L
        bal = _L.get_balance(uid)
    except Exception as e:
        log.exception("[vt_web] wallet read failed: %s", e)
        return json_err("wallet_unavailable", "The wallet service is not answering.", 503)

    # WEB_ATTACK finding 6: this used to be `_L.list_holds("estates", uid, ...)`, one
    # service, while `held` above comes from `get_balance`, which sums ALL of them. A
    # 7,000c osentar collateral hold next to a 5,000c estates bid rendered held=12,000
    # over drawer rows adding up to 5,000 — and the drawer's entire job is to answer
    # "what is holding MY coins".
    #
    # `hub_web._open_holds` already crosses the service boundary for exactly this
    # question, and documents why it is allowed to: read-only, own wallet, mutates
    # nothing. Reuse it rather than copying its query — two copies of a cross-service
    # read is two things to get wrong.
    try:
        import hub_web as _hub
        holds = _hub._open_holds(uid)
    except Exception as e:
        log.warning("[vt_web] hold list read failed for %s: %s", uid, e)
        holds = []

    rows = []
    for h in holds:
        title, sub = _describe_hold(h, uid)
        rows.append({"hold_id": h["hold_id"], "amount": int(h["amount"]),
                     "title": title, "sub": sub, "color": _hold_colour(h.get("reason"))})
    rows.sort(key=lambda r: -r["amount"])

    # The invariant the drawer is now able to state, and therefore must: these rows
    # ARE the `held` figure printed above them. When they are not — a read that failed
    # and returned nothing, a hold past the 100-row cap, a service this build cannot
    # describe — the drawer says so in a muted line instead of printing a total it
    # cannot account for. A silent shortfall here is a "my coins vanished" ticket.
    holds_sum = sum(r["amount"] for r in rows)
    unaccounted = int(bal["held"]) - holds_sum

    savings = None
    loan_out = None
    apr = None
    bank_error = None
    try:
        import banking_web as _bank
        snap = await _bank.strip_figures(uid)
        savings = snap.get("savings")
        loan_out = snap.get("loan_outstanding")
        apr = snap.get("savings_apr")
    except Exception as e:
        bank_error = str(e) or "Osentar Bank unreachable"

    net = None
    if savings is not None and loan_out is not None:
        net = int(bal["available"]) + int(bal["held"]) + int(savings) - int(loan_out)

    return json_ok(available=int(bal["available"]), held=int(bal["held"]),
                   balance=int(bal["balance"]), holds=rows,
                   holds_sum=holds_sum, unaccounted=unaccounted,
                   savings=savings, savings_apr=apr, loan_outstanding=loan_out,
                   net=net, bank_error=bank_error)


def _describe_hold(h: dict, uid: str) -> tuple:
    """Turn a ledger hold into the two lines a human reads. Real names, never ids.

    The reason string is the only link back to the domain object, so this resolves it:
    `realestate:bid:<listing_id>` becomes the lot's title and its closing time,
    `estates:market:<id>:stake` becomes the market question and the outcome staked on.
    A hold we cannot name still renders — with its reason and its expiry — because a
    coin reserved by something the page cannot explain is exactly the row a player
    needs to see.
    """
    reason = str(h.get("reason") or "")
    expires = h.get("expires_at") or ""
    tail = f"expires {expires[:16].replace('T', ' ')} UTC" if expires else "no expiry recorded"
    try:
        if reason.startswith("realestate:bid:"):
            lid = int(reason.rsplit(":", 1)[1])
            import Restocker_db as _db
            lot = _db.get_land_listing(lid) or {}
            name = lot.get("title") or f"Lot #{lid}"
            lead = "you are the high bidder" if str(lot.get("current_bidder") or "") == str(uid) else "bid placed"
            return f"Auction #{lid} — {name}", f"{lead} · {tail}"
        if reason.startswith("estates:market:"):
            mid = int(reason.split(":")[2])
            import estates_db as _edb
            mk = _edb.get_market(mid) or {}
            return (f"Prediction market #{mid} — {mk.get('title') or 'market'}",
                    f"stake held, not spent · {tail}")
    except Exception:
        pass
    # Not one of ours to name. Now that the drawer lists every service's holds, say
    # which service reserved it — "Osentar Bank — loan:collateral" is a row a player
    # can act on; a bare reason string from a section they have never opened is not.
    label = str(h.get("service_label") or h.get("service") or "").strip()
    if label and reason:
        return (f"{label} — {reason}", tail)
    return (reason or label or "Reserved", tail)


def register_shell_routes(app) -> None:
    """Attach the shared strip route. Idempotent — safe if both sections call it."""
    if web is None:  # pragma: no cover
        log.warning("[vt_web] aiohttp unavailable — shell not registered.")
        return
    existing = set()
    for r in app.router.routes():
        try:
            existing.add(r.resource.canonical)
        except Exception:
            pass
    if "/api/wallet/strip" not in existing:
        app.router.add_get("/api/wallet/strip", _handle_strip)
    log.info("[vt_web] shell v%s registered (/api/wallet/strip)", SHELL_VERSION)
