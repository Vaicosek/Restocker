"""
banking_web.py — the Bank of Osentar section of the V Tech site.

Mounted the way `bank_api` is mounted: a module that exposes
`register_banking_routes(app)`, imported and called in the `try/except` block in
`Restocker_web.start_webserver` immediately before `web.AppRunner` (:4688-4692).
Nothing is appended to the flat route list at :4630.

WHAT THIS SECTION IS
--------------------
`mock_banking.html` is the specification. Four member panels and two staff panels:

  * savings, with the accrued interest and the credit ladder behind it
  * the active loan, with its repayment schedule
  * the bond ladder by maturity
  * the earned credit limit, with the track record that produced it
  * (staff) the loan approval queue
  * (staff) collections

Transactional: deposit, withdraw, repay, buy a bond, redeem a bond. Every one is
preview → confirm → receipt, and the preview names the figures it is about to move.

═══════════════════════════════════════════════════════════════════════════════
OSENTAR IS A SEPARATE BOT. THIS IS THE CONTRACT.
═══════════════════════════════════════════════════════════════════════════════
Osentar Bank runs its own process and its own `bank.db`. It is the book of record for
savings, loans, bonds and credit policy. Restocker owns the ONLY coin wallet, and
Osentar moves coins by calling core's ledger (it holds `wallet.mint`, `wallet.transfer`
and `wallet.flag` — `ledger_v2.SERVICE_SCOPES`). This module never touches the bank's
book and never moves a coin: it renders what Osentar reports and forwards instructions.

Transport
    Base URL  `OSENTAR_BASE_URL`   e.g. http://127.0.0.1:8090
    Auth      `X-Osentar-Token: <OSENTAR_API_TOKEN>`   (shared secret, constant-time
              compared at the bank; this module never puts it in a URL or a log line)
    Version   `X-Osentar-API: 1`   — the bank refuses a major it does not implement,
              with 400 `api_version`. Equality checks on version strings are what
              OSENTAR_MIGRATION.md §1 is about: this is a MAJOR, not a build number.
    Timeout   `OSENTAR_TIMEOUT` seconds, default 6. A bank that has not answered in six
              seconds has not answered.
    Shape     every response is JSON `{"ok": true, ...}` or
              `{"ok": false, "code": "<machine>", "error": "<human>"}`.

Identity
    The user id is passed server-to-server in the query string (GET) or body (POST),
    and it is ALWAYS the session's Discord id read from the cookie by this module. The
    browser never supplies it. Osentar must treat that field as authoritative and must
    not accept requests without the shared token.

Endpoints this module calls
    GET  /api/v1/health
         -> {ok, service:"osentar-bank", version, ts}

    GET  /api/v1/account?user_id=<id>
         -> {ok,
             savings:{balance, apr, opened, last_paid, next_pay,
                      accrued_this_month, accrued_since_last_paid, accrued_lifetime,
                      avg90, ladder:[[iso_date, balance, credit], ...]},
             loan:  null | {id, principal, apr, terms, first, disbursed,
                            paid_principal, paid_interest, paid_count, last_paid_on,
                            outstanding, accrued_interest, payoff_today, closed,
                            schedule:[{seq, due, principal, interest, total,
                                       balance_after, status}, ...]},
             bonds: [{id, face, apr, bought, matures, term_days,
                      interest_at_maturity, earned_so_far, redeem_value_today,
                      early_redemption_penalty, matured}],
             bond_terms: [{term_days, apr, min_face, max_face}],
             record:{repaid_clean, late, defaults, late_detail, since},
             limit: {amount, cap, rounded_to, headroom,
                     components:[[label, coins], ...]}}

         `schedule`, `ladder`, `limit.components` and `interest_at_maturity` are
         COMPUTED BY THE BANK and rendered verbatim. This module does not re-derive a
         repayment schedule or a credit limit from the terms. Two implementations of one
         policy is how a FAQ says 7.5% while the embed says 10% (LEDGER_API_v2 §10) —
         so there is one implementation, it is the bank's, and if the bank does not
         send a schedule the panel says so instead of inventing one.

    POST /api/v1/savings/deposit   {user_id, amount, idempotency_key}
    POST /api/v1/savings/withdraw  {user_id, amount, idempotency_key}
    POST /api/v1/loan/repay        {user_id, loan_id, amount, idempotency_key}
    POST /api/v1/bond/buy          {user_id, face, term_days, idempotency_key}
    POST /api/v1/bond/redeem       {user_id, bond_id, idempotency_key}
         -> {ok, applied, ...figures, balance:{available, savings}}
         All five are idempotent on `idempotency_key`; a replay returns the ORIGINAL
         result with `"deduped": true` and must not act twice. The key is the one this
         page minted when it rendered — the caller mints it, per the house rule, and it
         is the same key end to end so a retry anywhere in the chain collapses.

    GET  /api/v1/staff/queue        -> {ok, requests:[{id, user_id, name, requested,
                                        terms, purpose, asked, outstanding_debt,
                                        limit, repaid_clean, late, frozen}]}
    GET  /api/v1/staff/collections  -> {ok, overdue:[{loan_id, user_id, name, days_late,
                                        owed, savings_reachable}]}
    POST /api/v1/staff/loan/decide  {request_id, decision:"approve"|"decline",
                                     actor_id, note, idempotency_key}
    POST /api/v1/staff/collect      {loan_id, amount, actor_id, idempotency_key}

WHEN OSENTAR IS DOWN
--------------------
`OsentarDown` is raised with a NAMED reason — "connection refused to
http://127.0.0.1:8090", "no answer in 6.0s", "answered HTTP 502", "not configured on
this server (OSENTAR_BASE_URL unset)". It is never a 500 and never a stack trace on a
page:

  * `GET /banking` renders, 200, with an amber panel naming the bank and the reason,
    and no fabricated figures anywhere on it.
  * the JSON endpoints return 503 `{"ok":false,"code":"bank_unreachable", ...}`.
  * the money strip on EVERY page still shows real `available` and `held` from core's
    ledger, shows savings as `unavailable`, and WITHHOLDS the net position rather than
    computing it from terms it does not have.
  * nothing else on the site is affected — Markets and Estates never call this module.

A failure also opens a short circuit breaker (`_COOLDOWN`), so one page render costs
one timeout rather than one per panel.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

try:
    from aiohttp import web
    import aiohttp
except Exception:  # pragma: no cover - aiohttp is a hard dep of the web server
    web = None  # type: ignore[assignment]
    aiohttp = None  # type: ignore[assignment]

import vt_web_shell as shell

log = logging.getLogger("banking_web")

BANKING_VERSION = "1.0"
OSENTAR_API_MAJOR = "1"

#: How long after a failure we stop dialling and reuse the named reason. Short enough
#: that the bank coming back is noticed within a page refresh or two.
_COOLDOWN_SECONDS = 15.0
_cooldown_until = 0.0
_cooldown_reason = ""


def _base_url() -> str:
    return os.getenv("OSENTAR_BASE_URL", "").strip().rstrip("/")


def _token() -> str:
    return os.getenv("OSENTAR_API_TOKEN", "").strip()


def _timeout() -> float:
    try:
        return float(os.getenv("OSENTAR_TIMEOUT", "6"))
    except (TypeError, ValueError):
        return 6.0


class OsentarDown(Exception):
    """The bank could not be reached or would not answer. Carries a human reason.

    Every construction site names the bank and what specifically happened, because
    "service unavailable" on a banking page is indistinguishable from "your money is
    gone" to the person reading it.
    """


async def osentar(method: str, path: str, *, params: Optional[dict] = None,
                  body: Optional[dict] = None) -> dict:
    """One call to Osentar. Returns the parsed body, or raises `OsentarDown`.

    A 4xx from the bank is NOT `OsentarDown` — the bank answered, and its refusal is
    the truth we should show the player ("you have no loan to repay"). Only transport
    failure and 5xx are down.
    """
    global _cooldown_until, _cooldown_reason
    base = _base_url()
    if not base:
        raise OsentarDown("Osentar Bank is not configured on this server "
                          "(OSENTAR_BASE_URL is unset).")
    if aiohttp is None:  # pragma: no cover
        raise OsentarDown("Osentar Bank is unreachable (no HTTP client available).")
    now = time.time()
    if now < _cooldown_until:
        raise OsentarDown(_cooldown_reason)

    url = base + path
    headers = {"X-Osentar-API": OSENTAR_API_MAJOR, "Accept": "application/json"}
    tok = _token()
    if tok:
        headers["X-Osentar-Token"] = tok
    tmo = aiohttp.ClientTimeout(total=_timeout())
    try:
        async with aiohttp.ClientSession(timeout=tmo) as sess:
            async with sess.request(method, url, params=params, json=body,
                                    headers=headers) as resp:
                if resp.status >= 500:
                    reason = f"Osentar Bank answered HTTP {resp.status}."
                    _cooldown_until = time.time() + _COOLDOWN_SECONDS
                    _cooldown_reason = reason
                    raise OsentarDown(reason)
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    reason = "Osentar Bank answered with something that was not JSON."
                    _cooldown_until = time.time() + _COOLDOWN_SECONDS
                    _cooldown_reason = reason
                    raise OsentarDown(reason)
    except OsentarDown:
        raise
    except Exception as e:
        # Timeout, DNS, connection refused, TLS — all "the bank is not there", and the
        # message names which one, because "unavailable" tells an operator nothing.
        kind = type(e).__name__
        if "Timeout" in kind:
            reason = f"Osentar Bank did not answer within {_timeout():.0f}s."
        elif "Connect" in kind or "Connection" in kind:
            reason = f"Could not connect to Osentar Bank at {base}."
        else:
            reason = f"Osentar Bank is unreachable ({kind})."
        _cooldown_until = time.time() + _COOLDOWN_SECONDS
        _cooldown_reason = reason
        log.warning("[banking] %s (%s)", reason, e)
        raise OsentarDown(reason)

    _cooldown_until = 0.0
    _cooldown_reason = ""
    if not isinstance(data, dict):
        raise OsentarDown("Osentar Bank answered in an unexpected shape.")
    return data


async def account(user_id: str) -> dict:
    """The member's whole bank position. One call, one snapshot, one point in time.

    Deliberately one call rather than four: a savings figure fetched at T and a loan
    figure fetched at T+300ms can disagree, and the page adds them together.
    """
    data = await osentar("GET", "/api/v1/account", params={"user_id": str(user_id)})
    if not data.get("ok"):
        raise OsentarDown(str(data.get("error") or "Osentar Bank refused the account read."))
    return data


async def strip_figures(user_id: str) -> dict:
    """`{savings, savings_apr, loan_outstanding}` for the money strip on every page.

    Called by `vt_web_shell._handle_strip`. Raises `OsentarDown` with the named reason,
    which the strip renders next to `savings: unavailable`.
    """
    data = await account(user_id)
    sav = data.get("savings") or {}
    loan = data.get("loan") or None
    return {
        "savings": int(sav.get("balance") or 0),
        "savings_apr": sav.get("apr"),
        "loan_outstanding": int((loan or {}).get("outstanding") or 0),
    }


# ══════════════════════════════════════════════════════════════════════════
# Wallet side — available coins live in core, not in the bank
# ══════════════════════════════════════════════════════════════════════════

def _wallet(user_id: str) -> dict:
    """`available`/`held` from core's ledger. `available = balance - held` (§4).

    A deposit is capped by AVAILABLE, never by balance: coins reserved by a live
    auction bid are not the player's to put in a savings account, and showing them as
    depositable is how a bid gets orphaned.
    """
    try:
        import ledger_v2 as _L
        b = _L.get_balance(str(user_id))
        return {"available": int(b["available"]), "held": int(b["held"]),
                "balance": int(b["balance"])}
    except Exception as e:
        log.exception("[banking] wallet read failed: %s", e)
        return {"available": 0, "held": 0, "balance": 0, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════
# Read endpoints
# ══════════════════════════════════════════════════════════════════════════

_MONEY_PURPOSES = ("deposit", "withdraw", "repay", "bond_buy", "bond_redeem")

#: purpose (what the form is for) -> endpoint (what `web_idempotency` is keyed by).
#: These must stay in step with the `_make_route(...)` calls at the bottom of this file:
#: the in-flight lookup below asks the idempotency table by ENDPOINT, and a mismatch
#: here would silently stop finding the stuck key and go back to minting a fresh one.
_MONEY_ENDPOINT = {p: f"banking:{p}" for p in _MONEY_PURPOSES}


#: Actions whose form key must name WHICH one — the subject rides in the purpose as
#: `<action>:<subject>` (see `shell.mint_form_key`). Deposit, withdraw and repay are
#: absent deliberately: there is one savings account and one open loan per player, the
#: caller names neither, so the user id already is the subject. A bond redemption does
#: name one, and a key that did not bind it let a player who read B-201's figures
#: redeem B-777 (WEB_ATTACK finding 7).
_SUBJECT_FIELD = {"bond_buy": "term_days", "bond_redeem": "bond_id"}


def _key_subject(value) -> str:
    """The subject half of a form-key purpose, normalised so MINT AND VERIFY AGREE.

    The estates counterpart is `estates_web._subject`; the rules differ on purpose, so
    the name does too. A lot id is always an integer there; a bank subject may be an
    opaque id minted by Osentar (`"B-201"`), which must survive normalisation intact.

    `estates_web._subject` has done this since finding 7 landed; the bank did not, and
    that asymmetry is WEB_VERIFY_R2 NEW-3. The render path minted from
    `str(int(term_days))` while the submit path used the raw body string, so a
    `term_days` arriving as `30.0` minted `bond_buy:30` and verified `bond_buy:30.0` —
    and a player confirming the bond he was shown was told he had confirmed a different
    bond term. One function, called from both sides, is the fix.

    A numeric subject is canonicalised (`30`, `"30"`, `" 30 "`, `30.0` are one term); a
    fractional one is not a subject we ever mint, so it becomes `"?"` and matches
    nothing. A NON-numeric subject is an opaque id from Osentar (`"B-201"`) and is only
    trimmed — dots and dashes are fine, because the key is split from the right.
    Anything that cannot be signed unambiguously is left for `shell.check_purpose` to
    refuse by name rather than silently folded onto `"?"`, which would make two
    different bad subjects into one purpose.
    """
    s = str(value if value is not None else "").strip()
    if not s:
        return "?"
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s
    if f != f or f in (float("inf"), float("-inf")) or f != int(f):
        # NaN, infinity, or a fraction. None of those is a bond term or an id.
        return "?"
    return str(int(f))


def _subject_purpose(action: str):
    """The purpose builder `money_post` calls with the submitted body."""
    field = _SUBJECT_FIELD[action]

    def build(body: dict) -> str:
        return f"{action}:{_key_subject(body.get(field))}"

    build.__name__ = f"purpose_{action}"
    return build


def _keys_for(uid: str, snap: dict) -> tuple:
    """`(keys, in_flight)` — the form key per action, and which actions are unresolved.

    NOT "mint five fresh keys". For each action we first ask whether this user already
    has a claimed-but-unfinished key on that endpoint, and if so we re-issue THAT key.

    For the two subject-bearing actions the value is not a key but a `{subject: key}`
    map — one key per bond on the ladder, one per term on offer — because a key that
    binds only "bond_redeem" is a key that redeems any bond the browser names. The
    in-flight re-issue is unchanged in kind: a stuck key still comes back instead of a
    fresh one, filed under the subject its own purpose names, so the reload still
    converges on the key Osentar has already seen.

    Why: the bank's idempotency key IS this form key, end to end. If a deposit POST
    times out with the instruction already applied at Osentar, the row stays
    `in_progress` (correctly — see `shell.mark_key_unknown`). If the reload then minted
    a fresh key, the player's second Confirm would carry a key Osentar has never seen,
    Osentar would not dedupe, and one intended 5,000c deposit would land twice. That is
    the bug this function exists to close: a reload converges on the SAME key, so the
    second submit is refused at our own claim (409 `in_flight`) and never reaches the
    bank at all.

    EVERY stuck row, filed under ITS OWN SUBJECT — not the oldest one. The single-row
    lookup this used to do was WEB_VERIFY_R2 NEW-1: with B-201 and B-777 both stuck at
    the bank, B-201 got its key back and B-777 was handed a FRESH one, which is a key
    Osentar has never seen for a bond Osentar may already have paid. The bank paid
    B-777 twice. A subject with an unresolved key gets that key or nothing; a fresh key
    is minted only for a subject with nothing outstanding.

    The second return value is not decoration. Handing back a key that is going to be
    refused, without saying so, is a worse experience than the double was — so the
    panel renders an "in flight — awaiting the bank" state naming the action AND the
    bond, and that one bond's flow refuses to open rather than walking the player into
    a 409. Per subject, again: one stuck redemption must not close redemption for every
    other bond the player holds (NEW-2). The panel is a courtesy, not the defence —
    the defence is that the same key comes back and our own claim refuses it.
    """
    keys, in_flight = {}, {}
    now = time.time()
    subjects = {
        "bond_redeem": [_key_subject(b.get("id")) for b in (snap.get("bonds") or [])],
        "bond_buy": [_key_subject(t.get("term_days")) for t in (snap.get("bond_terms") or [])],
    }
    for p in _MONEY_PURPOSES:
        # subject -> the row still outstanding for it. Oldest wins if a subject somehow
        # has two: re-issuing the oldest can never mint something the bank has not seen.
        stuck = {}
        for row in shell.in_flight_keys(uid, _MONEY_ENDPOINT[p]):
            stuck.setdefault(row["subject"], row)
        if stuck:
            in_flight[p] = {
                # The oldest, for the pre-subject shape callers already read.
                "since": min(r["created_at"] for r in stuck.values()),
                "age_seconds": max(0, int(now - min(r["created_at"] or now
                                                    for r in stuck.values()))),
                "reason": next(iter(stuck.values()))["note"],
                # The gate the browser actually uses: as fine as the key itself.
                "subjects": {
                    s: {"subject": s,
                        "since": r["created_at"],
                        "age_seconds": max(0, int(now - (r["created_at"] or now))),
                        "reason": r["note"]}
                    for s, r in stuck.items()},
            }
        if p not in _SUBJECT_FIELD:
            # No subject: the purpose is bare, so a stuck row is filed under "".
            row = stuck.get("") or (next(iter(stuck.values())) if stuck else None)
            keys[p] = row["key"] if row else shell.mint_form_key(uid, p)
            continue
        # One key per subject. A stuck key names its own subject in its purpose
        # (`bond_redeem:B-201`), so it is filed there and that bond alone re-offers it.
        per = {}
        for s in subjects[p]:
            if s in stuck:
                per[s] = stuck[s]["key"]
                continue
            try:
                per[s] = shell.mint_form_key(uid, f"{p}:{s}")
            except shell.UnkeyableSubject as e:
                # The subject came from Osentar and cannot be signed unambiguously.
                # No key is better than an ambiguous one: the row renders without a
                # key and a submit is refused as `bad_form_key`, nothing moves.
                log.warning("[banking] no form key for %s:%r — %s", p, s, e)
        for s, row in stuck.items():
            if s and s not in per:
                # The stuck subject has left the ladder (the bond redeemed, the term
                # withdrawn). Keep it visible rather than dropping a key that is still
                # unresolved at the bank.
                per[s] = row["key"]
        keys[p] = per
    return keys, in_flight


async def h_summary(request):
    """Everything the member panels render, plus the wallet side and the form keys.

    The idempotency keys for every money action on this page are minted HERE, at render
    time, and handed to the browser with the data. That is the house rule: the caller
    mints the key, from the page that is about to act, before anything is submitted.

    With one exception, and it is the important one: an action whose previous key is
    still unresolved does NOT get a new key (`_keys_for`). It gets the old one back,
    plus an entry in `in_flight` so the page can say so out loud.
    """
    sess, refusal = shell.require_session(request)
    if refusal is not None:
        return refusal
    uid = str(sess["user_id"])
    wallet = _wallet(uid)
    try:
        data = await account(uid)
    except OsentarDown as e:
        return shell.json_err("bank_unreachable", str(e), 503,
                              service="osentar", wallet=wallet)
    keys, in_flight = _keys_for(uid, data)
    return shell.json_ok(wallet=wallet,
                         in_flight=in_flight,
                         savings=data.get("savings") or {},
                         loan=data.get("loan"),
                         bonds=data.get("bonds") or [],
                         bond_terms=data.get("bond_terms") or [],
                         record=data.get("record") or {},
                         limit=data.get("limit") or {},
                         keys=keys,
                         staff=shell.is_staff(sess))


async def h_staff_queue(request):
    """The loan approval queue. Staff only — a non-staff session gets 403, not an
    empty list, because an empty list reads as "no applications" and that is a lie."""
    sess, refusal = shell.require_session(request)
    if refusal is not None:
        return refusal
    if not shell.is_staff(sess):
        return shell.json_err("not_staff", "This is a staff view.", 403)
    try:
        data = await osentar("GET", "/api/v1/staff/queue")
    except OsentarDown as e:
        return shell.json_err("bank_unreachable", str(e), 503, service="osentar")
    if not data.get("ok"):
        return shell.json_err(str(data.get("code") or "bank_refused"),
                              str(data.get("error") or "Osentar refused."), 400)
    uid = str(sess["user_id"])
    requests = data.get("requests") or []
    # One key per REQUEST. A single queue-wide key approved whichever request id the
    # browser sent, which is finding 7 with a staff member's hand on it.
    return shell.json_ok(requests=requests,
                         keys={_key_subject(r.get("id")): shell.mint_form_key(
                             uid, f"staff_decide:{_key_subject(r.get('id'))}")
                               for r in requests})


async def h_staff_collections(request):
    """Overdue loans and how much of each is reachable in savings."""
    sess, refusal = shell.require_session(request)
    if refusal is not None:
        return refusal
    if not shell.is_staff(sess):
        return shell.json_err("not_staff", "This is a staff view.", 403)
    try:
        data = await osentar("GET", "/api/v1/staff/collections")
    except OsentarDown as e:
        return shell.json_err("bank_unreachable", str(e), 503, service="osentar")
    if not data.get("ok"):
        return shell.json_err(str(data.get("code") or "bank_refused"),
                              str(data.get("error") or "Osentar refused."), 400)
    uid = str(sess["user_id"])
    overdue = data.get("overdue") or []
    return shell.json_ok(overdue=overdue,
                         keys={_key_subject(r.get("loan_id")): shell.mint_form_key(
                             uid, f"staff_collect:{_key_subject(r.get('loan_id'))}")
                               for r in overdue})


# ══════════════════════════════════════════════════════════════════════════
# Preview — server-computed figures, freshly read, nothing moved
# ══════════════════════════════════════════════════════════════════════════

_ACTIONS = ("deposit", "withdraw", "repay", "bond_buy", "bond_redeem")


async def h_preview(request):
    """The figures a confirm screen shows. Read-only, re-read at the moment of asking.

    Deliberately NOT computed in the browser from the page's initial load: by the time
    somebody opens the confirm dialog, a week's interest may have accrued and an auction
    hold may have moved `available`. The preview is a fresh read, and the commit is
    another one — the preview is a courtesy, the commit is the authority.
    """
    sess, refusal = shell.require_post_session(request)
    if refusal is not None:
        return refusal
    body = await shell.read_json(request)
    shell.note_body_identity(request, body, sess)
    uid = str(sess["user_id"])
    action = str(body.get("action") or "")
    if action not in _ACTIONS:
        return shell.json_err("bad_action", "Unknown action.", 400)
    try:
        amount = shell.coins(body.get("amount", 0)) if action != "bond_redeem" else 0
    except ValueError as e:
        return shell.json_err("bad_amount", str(e), 400)

    wallet = _wallet(uid)
    try:
        data = await account(uid)
    except OsentarDown as e:
        return shell.json_err("bank_unreachable", str(e), 503, service="osentar")

    try:
        preview = _build_preview(action, amount, body, wallet, data)
    except ValueError as e:
        return shell.json_err("not_previewable", str(e), 400)
    return shell.json_ok(**preview)


def _c(v) -> str:
    return f"{int(v):,}c"


def _build_preview(action: str, amount: int, body: dict, wallet: dict, data: dict) -> dict:
    """Turn a fresh snapshot into the rows the confirm screen shows.

    Every row is a figure with a unit. `blocked` is set when the action cannot proceed,
    and the reason is a row on the same screen as the disabled button — not a toast that
    appears after the click.
    """
    sav = data.get("savings") or {}
    loan = data.get("loan") or None
    avail = int(wallet.get("available") or 0)
    held = int(wallet.get("held") or 0)
    sbal = int(sav.get("balance") or 0)

    if action == "deposit":
        blocked = amount > avail or amount <= 0
        return {
            "head": "Preview — nothing has moved yet",
            "rows": [
                ["Into savings", _c(amount), "num"],
                ["Wallet available now", _c(avail), "num"],
                ["Held by open escrow (not depositable)", _c(held), "num amb"],
                ["Savings now", _c(sbal), "num"],
                ["Rate", f"{sav.get('apr', '—')}% APR", ""],
            ],
            "total": ["Savings after", _c(sbal + amount), "color:var(--green)"],
            "effect": [["Wallet available after", _c(avail - amount), ""]],
            "blocked": blocked,
            "confirm_label": f"Deposit {_c(amount)}",
            "note": ("Interest is credited monthly by the bank, on the ladder shown on "
                     "this page. This screen does not credit it."),
        }

    if action == "withdraw":
        blocked = amount > sbal or amount <= 0
        return {
            "head": "Preview — nothing has moved yet",
            "rows": [
                ["Out of savings", _c(amount), "num"],
                ["Savings now", _c(sbal), "num"],
                ["Interest accrued since last credit", _c(sav.get("accrued_since_last_paid") or 0), "num amb"],
            ],
            "total": ["Savings after", _c(sbal - amount), ""],
            "effect": [["Wallet available after", _c(avail + amount), "color:var(--green)"]],
            "blocked": blocked,
            "confirm_label": f"Withdraw {_c(amount)}",
            "note": ("Accrued interest is not withdrawn here — the bank credits it on its "
                     "own schedule and a withdrawal today does not forfeit it."),
        }

    if action == "repay":
        if not loan:
            raise ValueError("You have no open loan.")
        accrued = int(loan.get("accrued_interest") or 0)
        outstanding = int(loan.get("outstanding") or 0)
        payoff = int(loan.get("payoff_today") or (outstanding + accrued))
        to_int = min(accrued, amount)
        to_prin = max(0, amount - to_int)
        left = max(0, outstanding - to_prin)
        blocked = amount > avail or amount > payoff or amount <= 0
        return {
            "head": f"Loan #{loan.get('id')} · {loan.get('apr')}% APR",
            "rows": [
                ["Payment", _c(amount), "num"],
                ["Applied to interest first", "−" + _c(to_int), "num amb"],
                ["Then to principal", "−" + _c(to_prin), "num"],
                ["Principal outstanding now", _c(outstanding), "num"],
                ["Settle in full today", _c(payoff), "num"],
            ],
            "total": ["Principal outstanding after",
                      _c(left) if left else "settled",
                      "color:var(--red)" if left else "color:var(--green)"],
            "effect": [["Wallet available after", _c(avail - amount), ""]],
            "blocked": blocked,
            "confirm_label": ("Settle loan in full" if amount >= payoff
                              else f"Pay {_c(amount)}"),
            "note": ("Interest is charged to the day. Paying more than the settlement "
                     "figure is refused rather than held as credit."),
        }

    if action == "bond_buy":
        try:
            term_days = int(body.get("term_days") or 0)
        except (TypeError, ValueError):
            raise ValueError("Pick a term.")
        term = next((t for t in (data.get("bond_terms") or [])
                     if int(t.get("term_days") or 0) == term_days), None)
        if term is None:
            raise ValueError("That bond term is not on offer.")
        apr = float(term.get("apr") or 0)
        interest = int(amount * apr / 100.0 * term_days / 365.0)
        blocked = (amount > avail or amount <= 0
                   or amount < int(term.get("min_face") or 0)
                   or (term.get("max_face") and amount > int(term["max_face"])))
        return {
            "head": f"{term_days}-day bond at {apr}% APR",
            "rows": [
                ["Face value", _c(amount), "num"],
                ["Term", f"{term_days} days", ""],
                ["Interest at maturity", _c(interest), "num up"],
                ["Wallet available now", _c(avail), "num"],
            ],
            "total": ["Repaid at maturity", _c(amount + interest), "color:var(--green)"],
            "effect": [["Wallet available after", _c(avail - amount), ""]],
            "blocked": blocked,
            "confirm_label": f"Buy {_c(amount)} bond",
            "note": ("A bond locks the face value until maturity. Redeeming early is "
                     "possible and the penalty is shown on the bond's own row."),
        }

    if action == "bond_redeem":
        bid = str(body.get("bond_id") or "")
        bond = next((b for b in (data.get("bonds") or []) if str(b.get("id")) == bid), None)
        if bond is None:
            raise ValueError("That bond is not on your ladder.")
        matured = bool(bond.get("matured"))
        value = int(bond.get("redeem_value_today") or 0)
        penalty = int(bond.get("early_redemption_penalty") or 0)
        full = int(bond.get("face") or 0) + int(bond.get("interest_at_maturity") or 0)
        return {
            "head": f"Bond {bond.get('id')} · {bond.get('apr')}% APR",
            "rows": [
                ["Face value", _c(bond.get("face") or 0), "num"],
                ["Matures", str(bond.get("matures") or "—"), ""],
                ["Interest earned so far", _c(bond.get("earned_so_far") or 0), "num up"],
                ["Early redemption penalty", "−" + _c(penalty),
                 "num down" if penalty else "num"],
                ["Value if held to maturity", _c(full), "num"],
            ],
            "total": ["Paid to your wallet today", _c(value), "color:var(--green)"],
            "blocked": False,
            "danger": not matured,
            "confirm_label": ("Redeem matured bond" if matured
                              else f"Redeem early for {_c(value)}"),
            "note": (("This bond has matured — redeeming pays the full amount." if matured
                      else f"This bond has NOT matured. Redeeming now gives up "
                           f"{_c(full - value)} against holding it to "
                           f"{bond.get('matures')}.")),
        }

    raise ValueError("Unknown action.")


# ══════════════════════════════════════════════════════════════════════════
# Commit — the authority. Every check runs again, here, at submit time.
# ══════════════════════════════════════════════════════════════════════════

def _receipt(ok: bool, big: str, big_sub: str, rows: list, note: str) -> dict:
    return {"ok": ok, "big": big, "big_sub": big_sub, "rows": rows, "note": note}


async def _commit_bank(action: str, uid: str, body: dict, key: str) -> tuple:
    """Re-check server-side, then forward to Osentar with the page's key.

    The same key the page minted goes to the bank as its `idempotency_key`, so a retry
    at any layer — browser, this module, the network — collapses to one bank operation.
    This module's own `web_idempotency` row and the bank's are two locks on one door,
    which is what makes a half-completed call safe to retry.
    """
    wallet = _wallet(uid)
    try:
        snap = await account(uid)
    except OsentarDown as e:
        # Nothing was sent. The key may be released so the player can try again once
        # the bank is back — this is a definite no-effect refusal.
        raise shell.NoEffect("bank_unreachable", str(e), 503)

    avail = int(wallet.get("available") or 0)
    sav = snap.get("savings") or {}
    loan = snap.get("loan") or None
    sbal = int(sav.get("balance") or 0)

    if action == "deposit":
        amount = shell.coins(body.get("amount", 0))
        if amount <= 0:
            raise shell.NoEffect("bad_amount", "Enter an amount above zero.")
        if amount > avail:
            raise shell.NoEffect(
                "insufficient",
                f"Only {_c(avail)} available. {_c(int(wallet.get('held') or 0))} is held "
                f"by open escrow and is not spendable.", 409)
        r = await osentar("POST", "/api/v1/savings/deposit",
                          body={"user_id": uid, "amount": amount, "idempotency_key": key})
        return _bank_receipt(r, f"{amount:,}c", "deposited into savings",
                             [["Savings after", _c(_after(r, "savings", sbal + amount)), "num"],
                              ["Wallet available after", _c(_after(r, "available", avail - amount)), "num"]],
                             "Interest is credited monthly, on the ladder on this page.")

    if action == "withdraw":
        amount = shell.coins(body.get("amount", 0))
        if amount <= 0:
            raise shell.NoEffect("bad_amount", "Enter an amount above zero.")
        if amount > sbal:
            raise shell.NoEffect("insufficient",
                                 f"Savings holds {_c(sbal)}; you asked for {_c(amount)}.", 409)
        r = await osentar("POST", "/api/v1/savings/withdraw",
                          body={"user_id": uid, "amount": amount, "idempotency_key": key})
        return _bank_receipt(r, f"{amount:,}c", "withdrawn to your wallet",
                             [["Savings after", _c(_after(r, "savings", sbal - amount)), "num"],
                              ["Wallet available after", _c(_after(r, "available", avail + amount)), "num"]],
                             "Accrued interest stays with the account.")

    if action == "repay":
        if not loan:
            raise shell.NoEffect("no_loan", "You have no open loan.")
        amount = shell.coins(body.get("amount", 0))
        payoff = int(loan.get("payoff_today")
                     or (int(loan.get("outstanding") or 0) + int(loan.get("accrued_interest") or 0)))
        if amount <= 0:
            raise shell.NoEffect("bad_amount", "Enter an amount above zero.")
        if amount > avail:
            raise shell.NoEffect("insufficient", f"Only {_c(avail)} available.", 409)
        if amount > payoff:
            raise shell.NoEffect("over_payment",
                                 f"{_c(payoff)} settles the loan in full today; "
                                 f"{_c(amount)} is more than that.")
        r = await osentar("POST", "/api/v1/loan/repay",
                          body={"user_id": uid, "loan_id": loan.get("id"),
                                "amount": amount, "idempotency_key": key})
        left = max(0, int(loan.get("outstanding") or 0) - max(0, amount - int(loan.get("accrued_interest") or 0)))
        return _bank_receipt(r, f"{amount:,}c", f"paid to loan #{loan.get('id')}",
                             [["Principal outstanding after",
                               _c(r.get("outstanding", left)) if r.get("ok") else "—", "num"]],
                             "Interest first, then principal — the split is on your receipt.")

    if action == "bond_buy":
        amount = shell.coins(body.get("amount", 0), field="face")
        try:
            term_days = int(body.get("term_days") or 0)
        except (TypeError, ValueError):
            raise shell.NoEffect("bad_term", "Pick a term.")
        if amount <= 0:
            raise shell.NoEffect("bad_amount", "Enter a face value above zero.")
        if amount > avail:
            raise shell.NoEffect("insufficient", f"Only {_c(avail)} available.", 409)
        if not any(int(t.get("term_days") or 0) == term_days
                   for t in (snap.get("bond_terms") or [])):
            raise shell.NoEffect("bad_term", "That bond term is not on offer.")
        r = await osentar("POST", "/api/v1/bond/buy",
                          body={"user_id": uid, "face": amount, "term_days": term_days,
                                "idempotency_key": key})
        return _bank_receipt(r, f"{amount:,}c", f"{term_days}-day bond bought",
                             [["Bond", str(r.get("bond_id") or "—"), ""],
                              ["Matures", str(r.get("matures") or "—"), ""],
                              ["Repaid at maturity", _c(r.get("repaid_at_maturity") or 0), "num up"]],
                             "The face value is locked until maturity.")

    if action == "bond_redeem":
        bid = str(body.get("bond_id") or "")
        bond = next((b for b in (snap.get("bonds") or []) if str(b.get("id")) == bid), None)
        if bond is None:
            raise shell.NoEffect("no_bond", "That bond is not on your ladder.")
        r = await osentar("POST", "/api/v1/bond/redeem",
                          body={"user_id": uid, "bond_id": bid, "idempotency_key": key})
        return _bank_receipt(r, _c(r.get("paid") or bond.get("redeem_value_today") or 0),
                             f"bond {bid} redeemed",
                             [["Face value", _c(bond.get("face") or 0), "num"],
                              ["Penalty", "−" + _c(r.get("penalty") or bond.get("early_redemption_penalty") or 0), "num down"]],
                             "Paid straight to your wallet.")

    raise shell.NoEffect("bad_action", "Unknown action.")


def _after(r: dict, field: str, fallback: int) -> int:
    bal = r.get("balance") or {}
    v = bal.get(field)
    return int(v) if v is not None else int(fallback)


def _bank_receipt(r: dict, big: str, big_sub: str, rows: list, note: str) -> tuple:
    """Shape the bank's answer into a receipt. A refusal is a receipt too.

    `deduped` from the bank means our retry reached a call it had already applied — the
    money moved exactly once, and the player is told that rather than being shown a
    second success they might act on.
    """
    if not r.get("ok"):
        return 400, _receipt(False, "Not done",
                             str(r.get("code") or "refused"), [],
                             str(r.get("error") or "Osentar refused the instruction."))
    if r.get("deduped"):
        note = "Already applied — this was a retry of a request the bank had completed. " + note
    return 200, _receipt(True, big, big_sub, rows, note)


def _action_handler(action: str):
    async def handler(sess, body, key):
        return await _commit_bank(action, str(sess["user_id"]), body, key)
    handler.__name__ = f"commit_{action}"
    return handler


def _make_route(action: str, purpose, endpoint: str):
    async def route(request):
        try:
            return await shell.money_post(request, endpoint, purpose, _action_handler(action))
        except OsentarDown as e:
            # Reached only if the bank dies between the two reads. The key stays
            # claimed and `in_progress`: the instruction may have landed, so the honest
            # answer to a retry is "in flight", not a second attempt.
            return shell.json_err("bank_unreachable", str(e), 503, service="osentar")
        except ValueError as e:
            return shell.json_err("bad_amount", str(e), 400)
    route.__name__ = f"h_{action}"
    return route


h_deposit = _make_route("deposit", "deposit", "banking:deposit")
h_withdraw = _make_route("withdraw", "withdraw", "banking:withdraw")
h_repay = _make_route("repay", "repay", "banking:repay")
# The two that name a subject take a purpose BUILDER, so the key is checked against the
# bond term or the bond id in this body — not merely against "a bond action".
h_bond_buy = _make_route("bond_buy", _subject_purpose("bond_buy"), "banking:bond_buy")
h_bond_redeem = _make_route("bond_redeem", _subject_purpose("bond_redeem"),
                            "banking:bond_redeem")


# ══════════════════════════════════════════════════════════════════════════
# Staff writes
# ══════════════════════════════════════════════════════════════════════════

async def h_staff_decide(request):
    """Approve or decline a loan request. Staff only, idempotent, figures on confirm."""
    sess = shell.session_user(request)
    if not shell.is_staff(sess):
        return shell.json_err("not_staff", "This is a staff view.", 403)

    async def handler(s, body, key):
        decision = str(body.get("decision") or "")
        if decision not in ("approve", "decline"):
            raise shell.NoEffect("bad_decision", "Decision must be approve or decline.")
        try:
            rid = int(body.get("request_id"))
        except (TypeError, ValueError):
            raise shell.NoEffect("bad_request_id", "Which request?")
        r = await osentar("POST", "/api/v1/staff/loan/decide",
                          body={"request_id": rid, "decision": decision,
                                "actor_id": str(s["user_id"]),
                                "note": str(body.get("note") or ""),
                                "idempotency_key": key})
        return _bank_receipt(r, ("Approved" if decision == "approve" else "Declined"),
                             f"request #{rid}",
                             [["Disbursed", _c(r.get("disbursed") or 0), "num"]],
                             "Recorded on the bank's book and in its audit log.")

    try:
        return await shell.money_post(
            request, "banking:staff_decide",
            lambda b: f"staff_decide:{_key_subject(b.get('request_id'))}",
            handler)
    except OsentarDown as e:
        return shell.json_err("bank_unreachable", str(e), 503, service="osentar")


async def h_staff_collect(request):
    """Collect arrears from a borrower's SAVINGS. The wallet is never reached.

    That boundary is the bank's whole promise: it can take back what it already holds,
    it cannot reach into a player's pocket, and it certainly cannot touch coins escrowed
    against an auction bid. The confirm screen says so and so does this docstring,
    because the next person to add "and the wallet too" should have to delete a sentence
    that explains why not.
    """
    sess = shell.session_user(request)
    if not shell.is_staff(sess):
        return shell.json_err("not_staff", "This is a staff view.", 403)

    async def handler(s, body, key):
        try:
            loan_id = int(body.get("loan_id"))
        except (TypeError, ValueError):
            raise shell.NoEffect("bad_loan_id", "Which loan?")
        amount = shell.coins(body.get("amount", 0))
        if amount <= 0:
            raise shell.NoEffect("bad_amount", "Enter an amount above zero.")
        r = await osentar("POST", "/api/v1/staff/collect",
                          body={"loan_id": loan_id, "amount": amount,
                                "actor_id": str(s["user_id"]), "idempotency_key": key})
        return _bank_receipt(r, _c(r.get("collected") or 0), f"collected on loan #{loan_id}",
                             [["Taken from savings", _c(r.get("collected") or 0), "num"],
                              ["Still in arrears", _c(r.get("arrears_after") or 0), "num down"]],
                             "Savings only. The wallet, escrow holds, stock and land were "
                             "not touched.")

    try:
        return await shell.money_post(
            request, "banking:staff_collect",
            lambda b: f"staff_collect:{_key_subject(b.get('loan_id'))}",
            handler)
    except OsentarDown as e:
        return shell.json_err("bank_unreachable", str(e), 503, service="osentar")
    except ValueError as e:
        return shell.json_err("bad_amount", str(e), 400)


# ══════════════════════════════════════════════════════════════════════════
# The page
# ══════════════════════════════════════════════════════════════════════════

_BODY = r"""
<div class="page-head">
  <div>
    <h1>Banking</h1>
    <div class="page-sub">Bank of Osentar · savings, credit and bonds. A separate service — this page reports what it says.</div>
  </div>
</div>
<div id="bankView"><div class="empty">Loading your position…</div></div>
"""

_JS = r"""
let B = null;

function bankDown(msg){
  return `<div class="bank-down">
    <div class="bd-h">Osentar Bank is not answering</div>
    <div class="bd-b">${esc(msg)}<br>
      Your wallet is unaffected — available coins and escrow holds are held by V Tech core
      and are shown correctly in the strip above. Savings, loans and bonds live on the
      bank's own book and cannot be shown until it answers.</div>
  </div>`;
}

function panelSavings(){
  const s = B.savings || {};
  const ladder = s.ladder || [];
  return `
  <div class="tile s7">
    <div class="tile-h">Savings</div>
    <div class="kgrid">
      <div class="kpi"><div class="lab">Balance</div><div class="kfig num">${cn(s.balance)}</div></div>
      <div class="kpi"><div class="lab">Rate</div><div class="kfig num">${s.apr != null ? s.apr + '%' : '—'}</div><div class="sub">APR, credited monthly</div></div>
      <div class="kpi"><div class="lab">Accrued this month</div><div class="kfig num amb">${cn(s.accrued_this_month)}</div><div class="sub">credited ${esc(fmtD(s.next_pay))}</div></div>
    </div>
    ${ladder.length ? `
    <div class="section-h">Every credit this account has had</div>
    <div class="tablewrap"><table><thead><tr><th>Credited</th><th class="right">Balance then</th><th class="right">Interest</th></tr></thead>
    <tbody>${ladder.map(r=>`<tr><td>${esc(fmtD(r[0]))}</td><td class="num right">${cn(r[1])}</td><td class="num right up">${cn(r[2])}</td></tr>`).join('')}
    <tr><td class="muted">Total credited</td><td></td><td class="num right up"><b>${cn(ladder.reduce((a,r)=>a+r[2],0))}</b></td></tr>
    </tbody></table></div>` : '<div class="empty">No interest credited yet.</div>'}
    <div class="row" style="gap:8px;margin-top:14px">
      <button class="btn" onclick="flowDeposit()">Deposit</button>
      <button class="btn ghost" onclick="flowWithdraw()">Withdraw</button>
    </div>
  </div>`;
}

function panelLoan(){
  const L = B.loan;
  if(!L) return `<div class="tile s5"><div class="tile-h">Loan</div>
    <div class="empty">No open loan.</div></div>`;
  const rows = L.schedule || [];
  return `
  <div class="tile s5">
    <div class="tile-h">Loan #${esc(L.id)}</div>
    <div class="kgrid">
      <div class="kpi"><div class="lab">Outstanding</div><div class="kfig num" style="color:var(--red)">${cn(L.outstanding)}</div></div>
      <div class="kpi"><div class="lab">Interest accrued</div><div class="kfig num amb">${cn(L.accrued_interest)}</div><div class="sub">since ${esc(fmtD(L.last_paid_on))}</div></div>
      <div class="kpi"><div class="lab">Settle today</div><div class="kfig num">${cn(L.payoff_today)}</div><div class="sub">${L.apr}% APR</div></div>
    </div>
    ${rows.length ? `
    <div class="section-h">Repayment schedule</div>
    <div class="tablewrap"><table><thead><tr><th>#</th><th>Due</th><th class="right">Principal</th><th class="right">Interest</th><th class="right">Total</th><th>State</th></tr></thead>
    <tbody>${rows.map(r=>`<tr><td class="num">${esc(r.seq)}</td><td>${esc(fmtD(r.due))}</td>
      <td class="num right">${cn(r.principal)}</td><td class="num right amb">${cn(r.interest)}</td>
      <td class="num right">${cn(r.total)}</td>
      <td><span class="tag ${r.status==='paid'?'ok':(r.status==='due'?'warn':'')}">${esc(r.status)}</span></td></tr>`).join('')}
    </tbody></table></div>`
    : '<div class="empty">The bank did not return a schedule for this loan.</div>'}
    <div class="row" style="gap:8px;margin-top:14px"><button class="btn" onclick="flowRepay()">Repay</button></div>
  </div>`;
}

function panelBonds(){
  const bs = (B.bonds || []).slice().sort((a,b)=>String(a.matures).localeCompare(String(b.matures)));
  return `
  <div class="tile s7">
    <div class="tile-h">Bond ladder</div>
    ${bs.length ? `
    <div class="tablewrap"><table><thead><tr><th>Bond</th><th class="right">Face</th><th class="right">APR</th>
      <th>Matures</th><th class="right">Earned</th><th class="right">Redeem today</th><th></th></tr></thead>
    <tbody>${bs.map(b=>`<tr>
      <td>${esc(b.id)}</td>
      <td class="num right">${cn(b.face)}</td>
      <td class="num right">${esc(b.apr)}%</td>
      <td>${esc(fmtD(b.matures))}<div class="tsub">${esc(rel(b.matures))}</div></td>
      <td class="num right up">${cn(b.earned_so_far)}</td>
      <td class="num right">${cn(b.redeem_value_today)}</td>
      <td class="right"><button class="btn ghost" onclick="flowRedeem('${esc(b.id)}')">${b.matured ? 'Redeem' : 'Redeem early'}</button></td>
    </tr>`).join('')}
    <tr><td class="muted">Total face</td><td class="num right"><b>${cn(bs.reduce((a,b)=>a+(b.face||0),0))}</b></td>
      <td colspan="5"></td></tr>
    </tbody></table></div>` : '<div class="empty">No bonds.</div>'}
    ${(B.bond_terms || []).length ? `
    <div class="section-h">Buy a bond</div>
    <div class="chips">${B.bond_terms.map(t=>
      `<button class="chip" onclick="flowBond(${t.term_days})">${t.term_days} days · ${t.apr}%</button>`).join('')}</div>`
    : ''}
  </div>`;
}

function panelLimit(){
  const L = B.limit || {}, R = B.record || {};
  const comps = L.components || [];
  return `
  <div class="tile s5">
    <div class="tile-h">Credit limit</div>
    <div class="kgrid">
      <div class="kpi"><div class="lab">Your limit</div><div class="kfig num">${cn(L.amount)}</div>
        <div class="sub">${L.cap != null ? 'cap ' + n(L.cap) + 'c' : ''}</div></div>
      <div class="kpi"><div class="lab">Headroom</div><div class="kfig num up">${cn(L.headroom)}</div>
        <div class="sub">after current debt</div></div>
    </div>
    ${comps.length ? `
    <div class="section-h">How it was earned</div>
    <div class="tablewrap"><table><tbody>
      ${comps.map(c=>`<tr><td>${esc(c[0])}</td><td class="num right ${c[1]<0?'down':''}">${c[1]<0?'−':''}${n(Math.abs(c[1]))}c</td></tr>`).join('')}
      <tr><td class="muted">Limit</td><td class="num right"><b>${cn(L.amount)}</b></td></tr>
    </tbody></table></div>` : '<div class="empty">The bank did not break this limit down.</div>'}
    <div class="section-h">Track record</div>
    <div class="tablewrap"><table><tbody>
      <tr><td>Loans repaid without a late payment</td><td class="num right">${n(R.repaid_clean)}</td></tr>
      <tr><td>Late payments on record</td><td class="num right ${R.late?'down':''}">${n(R.late)}</td></tr>
      <tr><td>Defaults</td><td class="num right ${R.defaults?'down':''}">${n(R.defaults)}</td></tr>
      <tr><td>Customer since</td><td class="right">${esc(fmtD(R.since))}</td></tr>
    </tbody></table></div>
    ${R.late_detail ? `<div class="holdnote">Most recent late payment: ${esc(R.late_detail)}</div>` : ''}
  </div>`;
}

async function renderBank(){
  const j = await get('/api/banking/summary');
  const v = document.getElementById('bankView');
  if(j.code === 'not_logged_in'){
    v.innerHTML = '<div class="empty">Log in to see your bank position.</div>';
    return;
  }
  if(j.code === 'bank_unreachable'){ v.innerHTML = bankDown(j.error); return; }
  if(!j.ok){ v.innerHTML = '<div class="empty">' + esc(j.error || 'Unavailable.') + '</div>'; return; }
  B = j;
  v.innerHTML = inFlightBanner() + '<div class="bento">' + panelSavings() + panelLoan() + panelBonds() + panelLimit() + '</div>'
    + (j.staff ? '<div id="staffView"></div>' : '');
  if(j.staff) renderStaff();
}

async function renderStaff(){
  const [q, c] = await Promise.all([get('/api/banking/staff/queue'), get('/api/banking/staff/collections')]);
  const v = document.getElementById('staffView');
  if(!v) return;
  if(q.code === 'bank_unreachable'){ v.innerHTML = bankDown(q.error); return; }
  const reqs = (q.requests || []), od = (c.overdue || []);
  v.innerHTML = `
  <div class="section-h">Staff · loan approval queue</div>
  ${reqs.length ? `<div class="tile s12"><div class="tablewrap"><table>
    <thead><tr><th>Who</th><th class="right">Asked</th><th class="right">Terms</th><th class="right">Their debt</th>
      <th class="right">Their limit</th><th>Record</th><th>Purpose</th><th></th></tr></thead>
    <tbody>${reqs.map(r=>`<tr>
      <td>${esc(r.name)}${r.frozen?' <span class="tag warn">frozen</span>':''}</td>
      <td class="num right">${cn(r.requested)}</td>
      <td class="num right">${n(r.terms)}</td>
      <td class="num right ${r.outstanding_debt?'down':''}">${cn(r.outstanding_debt)}</td>
      <td class="num right ${r.requested > r.limit ? 'down' : ''}">${cn(r.limit)}</td>
      <td class="num">${n(r.repaid_clean)} clean · ${n(r.late)} late</td>
      <td class="muted">${esc(r.purpose)}</td>
      <td class="right"><button class="btn" onclick="flowDecide(${r.id},'approve')">Approve</button>
        <button class="btn ghost" onclick="flowDecide(${r.id},'decline')">Decline</button></td>
    </tr>`).join('')}</tbody></table></div></div>`
   : '<div class="empty">Nothing waiting.</div>'}

  <div class="section-h">Staff · collections</div>
  ${od.length ? `<div class="tile s12"><div class="tablewrap"><table>
    <thead><tr><th>Loan</th><th>Who</th><th class="right">Days late</th><th class="right">Owed</th>
      <th class="right">Reachable in savings</th><th></th></tr></thead>
    <tbody>${od.map(r=>`<tr>
      <td class="num">#${n(r.loan_id)}</td><td>${esc(r.name)}</td>
      <td class="num right down">${n(r.days_late)}</td>
      <td class="num right">${cn(r.owed)}</td>
      <td class="num right">${cn(r.savings_reachable)}</td>
      <td class="right"><button class="btn" onclick="flowCollect(${r.loan_id}, ${Math.min(r.owed, r.savings_reachable)})">Collect</button></td>
    </tr>`).join('')}</tbody></table></div>
    <div class="holdnote">Collections reach <b>savings only</b>. The wallet is never touched —
      not available coins, not escrow holds, not stock or land.</div></div>`
   : '<div class="empty">Nothing overdue.</div>'}`;
  /* One key per row, keyed by request id / loan id — never one key for the table. */
  window.SK = {decide:q.keys || {}, collect:c.keys || {}};
}

/* ---------- in flight — awaiting the bank ---------- */
/* An action whose last instruction never came back. We do not know whether the bank
   applied it, so we neither retry it nor pretend it failed: the same key is held, the
   action is named here, and its flow refuses to open. */
const IF_LABEL = {deposit:'A deposit', withdraw:'A withdrawal', repay:'A loan repayment',
                  bond_buy:'A bond purchase', bond_redeem:'A bond redemption'};
/* Flattened one row per STUCK SUBJECT, not per action: two bonds stuck at the bank are
   two sentences, and the third bond is not mentioned because it is not stuck. */
function inFlightList(){
  const f = (B && B.in_flight) || {};
  const out = [];
  Object.keys(f).forEach(a => {
    const subs = f[a] && f[a].subjects;
    if(subs && Object.keys(subs).length)
      Object.keys(subs).forEach(s => out.push([a, subs[s]]));
    else out.push([a, f[a]]);
  });
  return out;
}
/* The gate is as fine as the key. One stuck bond_redeem must close redemption for THAT
   bond only — a coarser gate closed every bond the player holds, permanently, since
   nothing clears it. Returns the stuck entry for this subject, or null. */
function inFlightFor(action, subject){
  const f = (B && B.in_flight || {})[action];
  if(!f) return null;
  const subs = f.subjects;
  if(!subs) return f;
  return subs[subject === undefined || subject === null ? '' : String(subject)] || null;
}
function mins(sec){
  const m = Math.floor((sec || 0) / 60);
  return m < 1 ? 'less than a minute' : (m === 1 ? 'a minute' : n(m) + ' minutes');
}
const IF_SUBJ = {bond_redeem:'bond ', bond_buy:'the '};
function inFlightNote(action, f){
  const s = f && f.subject ? ' for ' + (IF_SUBJ[action] || '') + f.subject
            + (action === 'bond_buy' ? '-day term' : '') : '';
  return (IF_LABEL[action] || 'An instruction') + s + ' you sent ' + mins(f.age_seconds)
    + ' ago has not been confirmed by Osentar Bank. It may have been applied. '
    + 'We are holding the same confirmation key for it, so it cannot be sent twice — '
    + 'and that is why ' + (s ? 'this one is' : 'this action is')
    + ' closed rather than retryable. '
    + 'Nothing clears this automatically today, so it stays this way until staff '
    + 'settle it by hand — ask staff to check it against your bank record.';
}
function inFlightBanner(){
  const rows = inFlightList();
  if(!rows.length) return '';
  return `<div class="bank-down">
    <div class="bd-h">In flight — awaiting the bank</div>
    <div class="bd-b">${rows.map(r => esc(inFlightNote(r[0], r[1]))).join('<br><br>')}</div>
  </div>`;
}

/* ---------- flows: every one previews on the server before it commits ---------- */
/* A bond action's key is minted per bond / per term, so it is looked up by the subject
   this flow is about. Sending another bond's key is refused by the server with
   `form_key_subject_mismatch` — the confirm screen and the instruction must agree. */
function actionKey(cfg){
  const k = B.keys[cfg.action];
  if(k && typeof k === 'object') return k[String(cfg.subject)] || '';
  return k;
}
function bankFlow(cfg){
  const stuck = inFlightFor(cfg.action, cfg.subject);
  if(stuck){
    /* Do not even price it. Opening the amount step would walk the player to a
       Confirm button that can only answer 409. */
    openFlow(Object.assign({}, cfg, {amountStep:false,
      preview: async () => ({ok:false, error: inFlightNote(cfg.action, stuck)})}));
    return;
  }
  openFlow(Object.assign({
    preview: async v => await post('/api/banking/preview',
      Object.assign({action:cfg.action, amount:v}, cfg.extra || {})),
    commit: async v => {
      const r = await post(cfg.url, Object.assign(
        {amount:v, idempotency_key:actionKey(cfg)}, cfg.extra || {}));
      if(r.replayed) r.note = 'This was a repeat of a request already completed — it was '
        + 'not done a second time. ' + (r.note || '');
      return r;
    }
  }, cfg));
}
function flowDeposit(){
  bankFlow({action:'deposit', url:'/api/banking/deposit',
    title:'Deposit into savings', sub:'Bank of Osentar', doneTitle:'Deposited',
    amountStep:true, amountLabel:'Wallet available', amountCap:B.wallet.available,
    chips:[['5,000c',5000],['25,000c',25000],['All available '+n(B.wallet.available)+'c',B.wallet.available]],
    check:v => v > B.wallet.available
      ? 'Only ' + n(B.wallet.available) + 'c available. Held coins are not spendable.' : '',
    amountRows:() => `<div class="kv"><span class="k">Held by open escrow</span>
      <span class="v num amb">${n(B.wallet.held)}c</span></div>`});
}
function flowWithdraw(){
  const s = B.savings.balance || 0;
  bankFlow({action:'withdraw', url:'/api/banking/withdraw',
    title:'Withdraw from savings', sub:'Bank of Osentar', doneTitle:'Withdrawn',
    amountStep:true, amountLabel:'In savings', amountCap:s,
    chips:[['5,000c',5000],['All '+n(s)+'c',s]],
    check:v => v > s ? 'Savings holds ' + n(s) + 'c.' : '',
    amountRows:() => ''});
}
function flowRepay(){
  const L = B.loan;
  bankFlow({action:'repay', url:'/api/banking/repay',
    title:'Repay loan #' + L.id, sub:'Bank of Osentar · ' + L.apr + '% APR', doneTitle:'Payment made',
    amountStep:true, amountLabel:'Wallet available', amountCap:B.wallet.available,
    chips:[['Settle in full ' + n(L.payoff_today) + 'c', L.payoff_today], ['5,000c', 5000]],
    check:v => v > B.wallet.available ? 'Only ' + n(B.wallet.available) + 'c available.'
             : v > L.payoff_today ? 'More than the ' + n(L.payoff_today) + 'c needed to settle in full.' : '',
    amountRows:() => `<div class="kv"><span class="k">Interest accrued</span>
        <span class="v num amb">${n(L.accrued_interest)}c</span></div>
      <div class="kv"><span class="k">Principal outstanding</span>
        <span class="v num">${n(L.outstanding)}c</span></div>`});
}
function flowBond(term){
  bankFlow({action:'bond_buy', url:'/api/banking/bond/buy', extra:{term_days:term},
    subject:term,
    title:'Buy a ' + term + '-day bond', sub:'Bank of Osentar', doneTitle:'Bond bought',
    amountStep:true, amountLabel:'Wallet available', amountCap:B.wallet.available,
    chips:[['5,000c',5000],['10,000c',10000],['25,000c',25000]],
    check:v => v > B.wallet.available ? 'Only ' + n(B.wallet.available) + 'c available.' : '',
    amountRows:() => ''});
}
function flowRedeem(id){
  bankFlow({action:'bond_redeem', url:'/api/banking/bond/redeem', extra:{bond_id:id},
    subject:id,
    title:'Redeem bond ' + id, sub:'Bank of Osentar', doneTitle:'Bond redeemed',
    amountStep:false});
}
function flowDecide(id, decision){
  openFlow({title:(decision==='approve'?'Approve':'Decline')+' request #'+id, sub:'Staff action',
    doneTitle:'Recorded', amountStep:false,
    preview: async () => ({ok:true, head:'Preview — nothing has moved yet',
      rows:[['Request','#'+id,''],['Decision',decision,'']],
      confirm_label:(decision==='approve'?'Approve and disburse':'Decline'),
      danger:decision!=='approve',
      note:decision==='approve'
        ? 'Approving disburses the loan from the bank treasury to the borrower\'s wallet.'
        : 'Declining closes the request. The borrower is told.'}),
    commit: async () => await post('/api/banking/staff/decide',
      {request_id:id, decision, idempotency_key:(window.SK.decide || {})[String(id)]})});
}
function flowCollect(loanId, suggested){
  openFlow({title:'Collect on loan #'+loanId, sub:'Staff action · savings only',
    doneTitle:'Collected', amountStep:true, amountLabel:'Reachable in savings',
    amountCap:suggested, amount:String(suggested),
    chips:[['All reachable '+n(suggested)+'c', suggested]],
    check:v => v > suggested ? 'Only ' + n(suggested) + 'c is reachable in savings.' : '',
    amountRows:() => '',
    preview: async v => ({ok:true, head:'Preview — nothing has moved yet',
      rows:[['Loan','#'+loanId,''],['Taken from savings', n(v)+'c','num']],
      confirm_label:'Collect '+n(v)+'c', danger:true,
      note:'Savings only. The wallet, escrow holds, stock and land are not touched.'}),
    commit: async v => await post('/api/banking/staff/collect',
      {loan_id:loanId, amount:v, idempotency_key:(window.SK.collect || {})[String(loanId)]})});
}

loadMe().then(() => { renderStrip(); renderBank(); });
"""


def _register_with_hub(key: str, label: str, path: str, order: int) -> None:
    """Tell `hub_web` this section exists, so the hub nav lists it.

    A mechanism built is not a mechanism wired: `hub_web` makes its nav a data
    registry precisely so a section can plug in without editing that file, and a
    section that never calls it is a page nothing links to.

    Called from `register_*_routes` rather than at import time, so it does not matter
    whether the hub is imported before or after this module — `_SECTIONS` is a
    module-level list and `register_section` is idempotent on `key`. Guarded, because
    the hub is not required for this section to work: without it the pages still serve,
    they are just not in the hub's nav.
    """
    try:
        import hub_web
    except Exception:
        return
    try:
        hub_web.register_section(key, label, path, order=order)
    except Exception as e:  # pragma: no cover
        log.warning("[%s] could not register with the hub nav: %s", key, e)


async def h_page(request):
    """The banking page. Logged out gets 401 and the sign-in card, not the chrome.

    Everything on this page is one player's own money. A logged-out render is a page of
    dashes and error boxes wrapped in a money strip that cannot show a figure — worse
    than useless as a first impression, and a rule violation besides. `hub_web` has
    always done this correctly; this section now does too.
    """
    _sess, refusal = shell.require_page_session(request)
    if refusal is not None:
        return refusal
    return shell.page("Banking", "banking", _BODY, _JS)


def register_banking_routes(app) -> None:
    """Attach the banking section. Mirrors `bank_api.register_bank_routes`."""
    if web is None:  # pragma: no cover
        log.warning("[banking] aiohttp unavailable — banking not registered.")
        return
    shell.register_shell_routes(app)
    _register_with_hub("banking", "Banking", "/banking", order=30)
    app.router.add_get("/banking", h_page)
    app.router.add_get("/api/banking/summary", h_summary)
    app.router.add_get("/api/banking/staff/queue", h_staff_queue)
    app.router.add_get("/api/banking/staff/collections", h_staff_collections)
    app.router.add_post("/api/banking/preview", h_preview)
    app.router.add_post("/api/banking/deposit", h_deposit)
    app.router.add_post("/api/banking/withdraw", h_withdraw)
    app.router.add_post("/api/banking/repay", h_repay)
    app.router.add_post("/api/banking/bond/buy", h_bond_buy)
    app.router.add_post("/api/banking/bond/redeem", h_bond_redeem)
    app.router.add_post("/api/banking/staff/decide", h_staff_decide)
    app.router.add_post("/api/banking/staff/collect", h_staff_collect)
    state = "configured" if _base_url() else "NOT CONFIGURED (OSENTAR_BASE_URL unset)"
    log.info("[banking] v%s registered — Osentar %s", BANKING_VERSION, state)
