"""
history_web.py — the per-user transaction history. READ-ONLY over tables that already exist.

WHY THIS EXISTS
───────────────
A player bought 5,000,000 coins of V Tech stock by paying John in-game, off the
exchange. John moved the shares by hand. A server moderator asked the obvious
question — *"where does this show the sale of stocks from you to him?"* — and the
honest answer was that it did not, because the website surfaced no per-user history at
all. `coin_ledger` and `stock_trade_log` have 138 and 12 rows in production and the web
layer never read either of them; grep `Restocker_web.py` for their names and you get
nothing. The buyer logs in, sees 5,001.15 shares in his portfolio, and no record of
where they came from.

So the goal is narrow and it is the whole thing: **both parties log in and see the same
true record of what happened.**

THE RULE THAT GOVERNS THIS FILE
───────────────────────────────
**Every row is read from data that already exists, and every row is labelled for what
it actually was.** An off-book transfer renders as an off-book transfer. It is never
dressed up as an exchange fill. Where a record was created later than the event it
describes, both dates are shown and both are labelled.

That is not manners, it is the entire value of the feature. A record that admits what
it is reads as bookkeeping. One that pretends to be something else reads as cover, and
the moment anybody pulls the thread it is worse than having nothing.

Consequences, enforced rather than intended:

* **No INSERT, UPDATE or DELETE anywhere in this module**, against any table. Every
  statement here is a SELECT. `test_history_web.py` parses this file's AST and fails
  the build if a write verb appears in any SQL string — a comment saying "read only"
  is not a guarantee, a test on the source is.
* **Nothing is backfilled.** The six source tables are read exactly as the bot left
  them. Two of them are empty in production today and one holds a single row, and this
  page renders that truthfully rather than filling the space.
* **A figure that is not recorded is not computed.** `stock_dividend_log` stores a
  per-share rate and a market total but no per-holder amount; this page shows the rate
  and the total and says the per-holder split is not recorded, instead of multiplying
  the rate by a holding measured today and presenting the product as history.
* **An unknown event date says unknown.** It never falls back to the row's write
  stamp wearing the event date's label. That single substitution is how an honest record
  becomes a misleading one, and it is the one mistake this feature cannot survive.
* **The row's write stamp is never displayed.** `recorded_at` is a bookkeeping artefact,
  not a date the reader can trust as history — printing it next to the event date invited
  exactly the misreading it was meant to prevent. It survives only as a sort key.

THE SIX SOURCES, AND WHAT EACH ONE HONESTLY SUPPORTS
────────────────────────────────────────────────────
| table                  | rows (prod) | renders as                    | whose |
|------------------------|-------------|-------------------------------|-------|
| `coin_ledger`          | 138 | wallet movement, +/-, reason made human | `user_id` |
| `stock_trade_log`      |  12 | exchange fill / liquidation            | `user_id` |
| `share_gifts`          |   1 | **OTC transfer**, in or out            | both sides |
| `stock_dividend_log`   |   0 | dividend declared on a market you hold | holders |
| `investor_payout_log`  |   0 | investor profit share                  | `user_id` |
| `hive_ledger`          |   3 | hive month for a market you work/own   | owner + harvesters |

`coin_ledger` is the only source that is a wallet movement, so it is the only source
that feeds the coins-in/coins-out totals. The others are events *about* money — a
dividend declaration, a month's hive production — and adding them to a coin total would
double-count the wallet credit that already sits in `coin_ledger` for the same event.
The totals strip says which figures it counts, on the page, in words.

`hive_ledger` has no `user_id` column: its `harvester_pay` is the total paid to *every*
harvester of that market that month. A harvester's own share is in their `coin_ledger`
rows. So the hive row is labelled a **market total** and is never presented as the
viewer's earnings, even though the viewer is only shown it because they are the owner
or one of the harvesters.

THE OTC ROW — the one that matters, and the one to get exactly right
────────────────────────────────────────────────────────────────────
`share_gifts` is the only table in this schema that records a transfer of shares
outside the exchange. Its single production row is the transfer this feature exists
for. It carries `created_at` (when the row was written) and a free-text `note` that
happens to state when the payment actually happened. `created_at` is a bookkeeping
stamp — when the bot got round to writing the row, sometimes days late and sometimes
backfilled by a repair script — so it is NOT a fact about the event and is NOT printed
anywhere on this page or its permalink. It is kept in the model because rows with no
event date still have to be ordered somehow. Only the event date is shown:

    09 Aug 2026   OTC TRANSFER   from TestIGN123   +5,001.15 shares · GreyHames
                  event date stated in the note as "09.08.26"

`_event_date_from_note` reads a date out of the note text and — this is the part that
matters — **reports the literal substring it read it from**, so a reader can check the
parse against the note printed directly underneath. It reads day-first (`09.08.26` is
9 August, the convention this economy's operator writes in) and it says so on the page,
because `09.08.26` is genuinely ambiguous and a silent choice between two readings of
an ambiguous date is exactly the kind of quiet substitution this file is against. When
no date can be read, the event date is the word **unknown** — never `created_at`.

IDENTITY
────────
`shell.session_user` and nothing else, on both routes. A user sees their own history
and nobody else's. There is no user id in any query string, path or body that this
module reads — the permalink addresses an *event*, and the check is "are you a party to
this event", made against the row, on every fetch.

THE PERMALINK
─────────────
`/history/e/{source}/{eid}` — one event, one page, so John can hand somebody a link.
Both parties to a two-sided event may open it; nobody else, including staff. The ids
are sequential and therefore guessable, and that is fine: this is not a capability URL.
Guessing an id gets a stranger the same 404 a nonexistent id gets, because the
authorisation is a property of the row and not a property of the address.

STAFF SEE NOTHING EXTRA
───────────────────────
Deliberately. Staff have `hub_attack_log`, the database, and the bot; what they do not
need is a web surface where one signed-in account can read another player's money
history, because that surface is one session-check bug away from being everybody's.
This site has not lost the "your own data only" property across four adversarial rounds
and a staff bypass is the cheapest way to lose it. The one thing staff would genuinely
want — a dispute view of a two-sided event — they already have: they are usually a
party to it, and if they are not, the participants can send them the permalink.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

try:
    from aiohttp import web
except Exception:  # pragma: no cover - aiohttp absent in a bare import check
    web = None  # type: ignore

import vt_web_shell as shell

log = logging.getLogger("history_web")

HISTORY_VERSION = "1.0"

#: Rows per page. The timeline is the whole point, so this is generous — but it is
#: bounded, because 138 rows today is 138 rows today.
PAGE_SIZE = 50

#: Per-source read ceiling. Every source is fetched whole and merged in Python, which
#: is the clear way to write a six-way heterogeneous merge and is trivially correct at
#: this scale. The ceiling is what keeps that true later: if a source hits it, the page
#: SAYS the history is truncated rather than quietly showing a prefix as if it were all.
SOURCE_CAP = 5000


# ══════════════════════════════════════════════════════════════════════════
# Lazy module handles — same convention as `estates_web` and `messages_web`, so this
# file imports and is testable without the bot present.
# ══════════════════════════════════════════════════════════════════════════

def _core_db():
    import Restocker_db as _db
    return _db


# ══════════════════════════════════════════════════════════════════════════
# Time — every stored timestamp in these six tables is TEXT, in two formats
# ══════════════════════════════════════════════════════════════════════════

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _ts(raw: Any) -> Optional[float]:
    """A stored timestamp -> epoch seconds, or **None** when it cannot be read.

    These tables hold two shapes, both real and both present in the production copy:
    `datetime('now')`'s `"2026-08-14 10:45:25"` (UTC, no zone marker) and Python's
    `datetime.now(timezone.utc).isoformat()`'s `"2026-08-16T12:39:31.637045+00:00"`.

    None is a real answer and it is propagated as one. A timestamp this function cannot
    read must not become "now", and it must not become the epoch — either would put a
    row at a time it did not happen, which is the failure this whole file is written
    against.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # `datetime('now')` in SQLite is UTC. Assuming local time here would shift
        # every bot-written row by the server's offset.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _date(ts: Optional[float]) -> str:
    """`"09 Aug 2026"`. Human, never ISO (UI_PRINCIPLES part 2), and never a raw epoch.

    An unreadable or absent timestamp is the WORD "unknown", not a dash and not a
    substitute date. A dash reads as "nothing here"; this reads as "we do not know",
    which is the true statement.
    """
    if ts is None:
        return "unknown"
    tm = time.gmtime(ts)
    return f"{tm.tm_mday:02d} {_MONTHS[tm.tm_mon - 1]} {tm.tm_year}"


def _stamp(ts: Optional[float]) -> str:
    """The exact time for a `title` tooltip — human, UTC, still not ISO."""
    if ts is None:
        return "not recorded"
    tm = time.gmtime(ts)
    return (f"{tm.tm_mday:02d} {_MONTHS[tm.tm_mon - 1]} {tm.tm_year} "
            f"{tm.tm_hour:02d}:{tm.tm_min:02d} UTC")


def _month_name(month: Any) -> str:
    """`"2026-08"` -> `"August 2026"`. A month column is a period, not a date, and it
    is labelled as one wherever it is shown."""
    s = str(month or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", s)
    if not m:
        return s or "unknown month"
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        return s
    full = ("January", "February", "March", "April", "May", "June", "July",
            "August", "September", "October", "November", "December")
    return f"{full[mo - 1]} {y}"


#: Dates that appear inside free text. Day-first and year-first only; see
#: `_event_date_from_note` for why there is no month-first pattern.
_NOTE_DATE_PATTERNS = (
    # 09.08.26 · 9/8/2026 · 09-08-2026
    (re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b"), "dmy"),
    # 2026-08-09
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    # 9 Aug 2026 · 9 August 2026
    (re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{4})\b",
                re.IGNORECASE), "dMy"),
)


def _event_date_from_note(note: Any) -> tuple:
    """`(epoch|None, literal_source_text, how)` — a date read out of free text.

    THE PROVENANCE IS RETURNED WITH THE VALUE, and the page prints both. A parsed date
    with no visible source is a claim; a parsed date next to the seven characters it
    came from, above the note it came from, is a citation the reader can check in one
    glance. That is the difference this whole file turns on.

    **Day-first, and the page says so.** `09.08.26` is 9 August under the convention
    this economy's operator writes in, and 8 September under the American one. There is
    no way to tell from the string. Guessing silently is unacceptable; refusing to read
    it at all throws away the only record of when the payment happened. So it is read
    day-first, the reading is stated on the page next to the raw text, and a reader who
    knows better can see immediately that they need to correct the note. A month-first
    pattern is deliberately absent rather than tried second — two patterns that both
    match would make the answer depend on ordering, invisibly.

    Returns `(None, "", "")` when nothing parses. That is the common case and it is
    fine: the caller renders the word "unknown".
    """
    s = str(note or "")
    if not s:
        return None, "", ""
    for rx, order in _NOTE_DATE_PATTERNS:
        m = rx.search(s)
        if not m:
            continue
        raw = m.group(0)
        try:
            if order == "dmy":
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if y < 100:
                    y += 2000
            elif order == "ymd":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                d = int(m.group(1))
                mo = _MONTHS.index(m.group(2)[:3].title()) + 1
                y = int(m.group(3))
            dt = datetime(y, mo, d, tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue
        how = {"dmy": "read day-first", "ymd": "read year-first",
               "dMy": "read from the month name"}[order]
        return dt.timestamp(), raw, how
    return None, "", ""


# ══════════════════════════════════════════════════════════════════════════
# Names — of people and of markets
# ══════════════════════════════════════════════════════════════════════════

def esc(s: Any) -> str:
    """Escape AT RENDER. Nothing in this module writes, so there is no other option —
    but it is said out loud because a `note` and a `reason` are free text written by a
    bot command, and both reach a page."""
    return html.escape(str(s if s is not None else ""), quote=True)


_NAMES_CACHE: dict = {}
_NAMES_AT = 0.0


def _names() -> dict:
    """`{user_id: display name}` from `stock_names.yml` — the same file the exchange
    and `messages_web` read. One source of display names on this site; a counterparty
    whose name differs between two pages is a support ticket."""
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


def _ign(uid: str) -> str:
    """The registered in-game name, or `""`. `ign_registry` is the one table that ties
    a Discord id to a name a Minecraft moderator can actually verify, which is the exact
    audience this feature was built for."""
    try:
        with _core_db().db() as conn:
            row = conn.execute("SELECT ign FROM ign_registry WHERE user_id = ? "
                               "ORDER BY registered_at LIMIT 1", (str(uid),)).fetchone()
        return str(row["ign"]) if row and row["ign"] else ""
    except Exception:
        return ""


def counterparty(uid: Any) -> dict:
    """`{"id", "name", "sub", "linked"}` for the other side of an event.

    DECISION — a name where there is one, the FULL Discord id where there is not.

    `messages_web._display_name` renders an unnamed player as "Unnamed player" and
    never shows an id, and that is right *there*: an inbox is a list of people you are
    already talking to, and an id fragment identifies nobody while looking like it
    should. This page is evidence. The buyer in the production row has no
    `stock_names.yml` entry and no registered IGN, and rendering him as "Unnamed
    player" on the one record of a five-million-coin transfer would produce exactly the
    document the moderator's question was about — a record that does not say who.

    So: display name, then IGN, then `investors.name`; and when none of the three
    exists, the full Discord id in mono, labelled as one. A full id is not a fragment —
    it resolves in Discord, it is what the bot logged, and it is visible to anyone in
    the server already. It identifies the party without inventing a name for them.

    Where a name IS known, the id still travels in the row's tooltip and is printed in
    full on the permalink page, so the two parties and whoever they hand the link to are
    looking at the same identifier rather than at two spellings of a nickname.
    """
    uid = str(uid or "")
    if not uid:
        return {"id": "", "name": "not recorded", "sub": "", "linked": False}
    if uid.startswith("treasury:"):
        return {"id": uid, "name": "V Tech treasury", "sub": uid, "linked": True}

    nm = _names().get(uid)
    nm = str(nm).strip() if nm else ""
    ign = _ign(uid)
    if not nm:
        try:
            with _core_db().db() as conn:
                row = conn.execute("SELECT name FROM investors WHERE user_id = ?",
                                   (uid,)).fetchone()
            if row and row["name"]:
                nm = str(row["name"]).strip()
        except Exception:
            pass
    if not nm:
        nm = ign
    if not nm:
        return {"id": uid, "name": f"Discord {uid}", "linked": False,
                "sub": "no in-game name linked to this account"}
    sub = f"in-game {ign}" if (ign and ign != nm) else ""
    return {"id": uid, "name": nm, "sub": sub, "linked": True}


_MARKETS_CACHE: dict = {}
_MARKETS_AT = 0.0


def _markets() -> dict:
    """`{market_id: {"name", "owner_id"}}`. Cached for a minute; it changes never."""
    global _MARKETS_CACHE, _MARKETS_AT
    if time.time() - _MARKETS_AT < 60 and _MARKETS_CACHE:
        return _MARKETS_CACHE
    out = {}
    try:
        with _core_db().db() as conn:
            for r in conn.execute("SELECT market_id, name, owner_id FROM markets"):
                out[str(r["market_id"])] = {"name": str(r["name"] or r["market_id"]),
                                            "owner_id": str(r["owner_id"] or "")}
    except Exception:
        log.warning("[history] market lookup failed", exc_info=True)
        return _MARKETS_CACHE
    _MARKETS_CACHE, _MARKETS_AT = out, time.time()
    return out


def market_name(mid: Any) -> str:
    """`"greyhames"` -> `"GreyHames"`. UI_PRINCIPLES: real names over internal ids,
    everywhere a person looks. An unknown id renders as itself rather than as a guess."""
    mid = str(mid or "")
    if not mid:
        return ""
    return (_markets().get(mid) or {}).get("name") or mid


def ticker(mid: Any) -> str:
    """The market's short code, derived EXACTLY as `hub_web._exchange_rows` derives it
    (`mid.upper()[:4]`), so a ticker never differs between two pages of this site. It
    is a derivation and not a stored field — `markets` has no ticker column — so it is
    only ever shown next to the full market name, never alone."""
    return str(mid or "").upper()[:4]


# ══════════════════════════════════════════════════════════════════════════
# Numbers
# ══════════════════════════════════════════════════════════════════════════

def c(v: Any, sign: bool = False) -> str:
    """Coins. Two decimals only when there are any — the production `value_coins` is
    `4999999.74` and rounding it to five million on the page would be inventing the very
    figure the record is about."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    s = f"{f:,.2f}".rstrip("0").rstrip(".") if abs(f - round(f)) > 1e-9 else f"{f:,.0f}"
    if sign and f > 0:
        s = "+" + s
    return s


def sh(v: Any) -> str:
    """Shares. Fractional to two places, because holdings genuinely are (`5001.15`)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{f:,.2f}"


# ══════════════════════════════════════════════════════════════════════════
# `coin_ledger.reason` — made human, with the raw string still reachable
# ══════════════════════════════════════════════════════════════════════════

def humanise_reason(reason: Any) -> tuple:
    """`reason` -> `(headline, detail)`, both plain text.

    Every shape here was measured on the production copy; none is speculative:

        ''                                     33 rows
        'hive:vtech:1537774138976894986'       a hive payout, keyed by Discord message
        'hive:vtech:export:vtech:1786704325496' a hive export payout
        'hive:reprice:wage-basis-correction'   a correction run
        'stock buy main' / 'stock sell greyhames'
        'order#46' / 'repair:order#22'
        'liquidation of <@517082724104929330>'
        'liquidation transfer -> 1203738126850461738'

    An unrecognised reason is returned VERBATIM as its own headline. Guessing at a
    string this function has never seen would be the same class of mistake as guessing
    at a date, and the raw text is more useful than a wrong summary. The raw string is
    also always available on the page — it is the row's tooltip and it is printed in
    full on the permalink — so the humanised form can be checked rather than trusted.

    An EMPTY reason (a quarter of the production table) is "No reason recorded". Not
    "adjustment", not "transfer": nothing was written down, and saying so is the only
    true thing available.
    """
    s = str(reason or "").strip()
    if not s:
        return "No reason recorded", "The bot wrote this movement without a reason."

    m = re.fullmatch(r"hive:([a-z0-9_-]+):export:[a-z0-9_-]+:(\d+)", s, re.I)
    if m:
        return (f"Hive export · {market_name(m.group(1))}",
                f"batch reference {m.group(2)}")
    m = re.fullmatch(r"hive:([a-z0-9_-]+):(\d{15,})", s)
    if m:
        return (f"Hive harvest payout · {market_name(m.group(1))}",
                f"harvest report {m.group(2)}")
    m = re.fullmatch(r"hive:reprice:(.+)", s)
    if m:
        return "Hive reprice", m.group(1).replace("-", " ")
    m = re.fullmatch(r"hive:([a-z0-9_-]+):(.+)", s, re.I)
    if m:
        return f"Hive · {market_name(m.group(1))}", m.group(2)
    m = re.fullmatch(r"stock (buy|sell) ([a-z0-9_-]+)", s, re.I)
    if m:
        return (f"Stock {m.group(1).lower()} · {market_name(m.group(2))}",
                "settled through the exchange")
    m = re.fullmatch(r"repair:order#(\d+)", s, re.I)
    if m:
        return f"Repair · shop order #{m.group(1)}", "a correction to an earlier order"
    m = re.fullmatch(r"order#(\d+)", s, re.I)
    if m:
        return f"Shop order #{m.group(1)}", ""
    m = re.fullmatch(r"liquidation of <@!?(\d+)>", s)
    if m:
        return f"Liquidation of {counterparty(m.group(1))['name']}", ""
    m = re.fullmatch(r"liquidation transfer -> (\d+)", s)
    if m:
        return f"Liquidation transfer to {counterparty(m.group(1))['name']}", ""
    return s, ""


def _market_of_reason(reason: Any) -> str:
    """The market a wallet movement belongs to, when the reason names one.

    Used ONLY by the market filter. It is a derivation, so it never becomes a rendered
    claim: no row says "this was a GreyHames movement" on the strength of it — the row
    prints the reason, and the filter uses this to decide whether to show the row.
    """
    s = str(reason or "").strip()
    for rx in (r"hive:([a-z0-9_-]+):", r"stock (?:buy|sell) ([a-z0-9_-]+)"):
        m = re.match(rx, s, re.I)
        if m and m.group(1) not in ("reprice",):
            return m.group(1).lower()
    return ""


# ══════════════════════════════════════════════════════════════════════════
# The sources. Six readers, one shape, and every one of them a SELECT.
# ══════════════════════════════════════════════════════════════════════════
#
# Each reader returns a list of event dicts:
#
#   source       str   the table this came from — half of the permalink
#   eid          str   the row's identity within it — the other half
#   kind         str   THE LABEL. What this event actually was.
#   event_at     float|None   when it HAPPENED. None means unknown, and stays None.
#   event_src    str   where the event date came from, in words, when it is not obvious
#   recorded_at  float|None   when the ROW was written
#   sort_at      float        ordering only — see `_sort_key`
#   market_id    str
#   coin_delta   float|None   a wallet movement, and only for `coin_ledger`
#   figures      list[(label, value_html, cls)]   the row's numbers, each with its unit
#   detail       str (escaped html)   the second line
#   note         str   free text as stored
#
# `kind` is not decoration: it is the promise that a row is labelled for what it was.
# There is no generic "transaction" kind and no row reaches the page without one.

_KINDS = {
    "coin_ledger": "WALLET",
    "stock_trade": "EXCHANGE",
    "otc": "OTC TRANSFER",
    "dividend": "DIVIDEND",
    "investor_payout": "INVESTOR PAYOUT",
    "hive": "HIVE MONTH",
}

#: Filter chips, in the order they are offered. `all` first because it is the default.
FILTER_TYPES = (
    ("all", "Everything"),
    ("coin_ledger", "Wallet"),
    ("stock_trade", "Exchange"),
    ("otc", "OTC transfers"),
    ("dividend", "Dividends"),
    ("investor_payout", "Investor payouts"),
    ("hive", "Hive months"),
)


def _rows(sql: str, args=()) -> list:
    """One SELECT, as dicts. A read that fails is an empty read and a logged error —
    never a partial page pretending to be a whole one; `_gather` reports the failure to
    the viewer."""
    with _core_db().db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def read_coin_ledger(uid: str) -> list:
    """Wallet movements. THE ONLY SOURCE THAT MOVES THE WALLET, and the only one with a
    stored running balance.

    `balance_after` is read, never recomputed. The rows are also walked in id order to
    see whether `previous.balance_after + delta` agrees with `balance_after`, and on the
    production copy **23 of 117 checkable rows disagree** — the ledger has movements
    written outside it. So the stored figure is what is shown, always, and a
    disagreement is DISCLOSED on the row rather than silently reconciled or silently
    ignored. A page that quietly recomputed would show 23 balances the bot never wrote.
    """
    rows = _rows("SELECT id, delta, balance_after, reason, created_at FROM coin_ledger "
                 " WHERE user_id = ? ORDER BY id DESC LIMIT ?", (str(uid), SOURCE_CAP))
    by_id = {int(r["id"]): r for r in rows}
    out = []
    for r in rows:
        rid = int(r["id"])
        at = _ts(r["created_at"])
        delta = float(r["delta"] or 0)
        head, det = humanise_reason(r["reason"])

        # The recomputation check, against the immediately preceding row OF THIS USER
        # that we actually hold. When the predecessor is off the end of the page there
        # is nothing to check against, and nothing is claimed.
        drift = None
        prev_ids = [i for i in by_id if i < rid]
        if prev_ids:
            prev = by_id[max(prev_ids)]
            recomputed = float(prev["balance_after"] or 0) + delta
            if abs(recomputed - float(r["balance_after"] or 0)) > 0.005:
                drift = recomputed

        out.append({
            "source": "coin_ledger", "eid": str(rid), "kind": _KINDS["coin_ledger"],
            "event_at": at, "event_src": "", "recorded_at": at,
            "market_id": _market_of_reason(r["reason"]),
            "coin_delta": delta,
            "headline": head, "detail_text": det,
            "note": str(r["reason"] or ""),
            "balance_after": float(r["balance_after"] or 0),
            "balance_drift": drift,
        })
    return out


def read_stock_trades(uid: str) -> list:
    """Exchange activity. `side` is NOT assumed to be buy or sell.

    The production table holds `liquidated` and `unliquidated` rows at
    `price_per_share = 0` and `total_coins = 0` — a forced unwind, not a fill. Calling
    those "sold for 0 coins" would be a lie of exactly the kind this file exists to
    avoid, so an unrecognised side is printed as the word the bot stored, and a row that
    moved no coins says it moved no coins instead of printing a zero price.
    """
    rows = _rows("SELECT id, market_id, side, shares, price_per_share, total_coins, "
                 "       traded_at FROM stock_trade_log "
                 " WHERE user_id = ? ORDER BY id DESC LIMIT ?", (str(uid), SOURCE_CAP))
    out = []
    for r in rows:
        at = _ts(r["traded_at"])
        side = str(r["side"] or "").strip().lower()
        mid = str(r["market_id"] or "")
        total = float(r["total_coins"] or 0)
        pps = float(r["price_per_share"] or 0)
        if side in ("buy", "sell"):
            head = f"Exchange {side} · {market_name(mid)}"
            det = (f"{sh(r['shares'])} shares at {c(pps)} c each · {c(total)} c total"
                   if total or pps else
                   f"{sh(r['shares'])} shares · no coins recorded against this fill")
        else:
            head = f"Shares {side or 'movement'} · {market_name(mid)}"
            det = (f"{sh(r['shares'])} shares · recorded by the bot as "
                   f"“{side or 'no side'}”, not an exchange fill"
                   + ("" if (total or pps) else " · no coins moved"))
        out.append({
            "source": "stock_trade", "eid": str(int(r["id"])), "kind": _KINDS["stock_trade"],
            "event_at": at, "event_src": "", "recorded_at": at,
            "market_id": mid, "coin_delta": None,
            "headline": head, "detail_text": det, "note": "",
            "shares": float(r["shares"] or 0), "price": pps, "total": total, "side": side,
        })
    return out


def read_otc(uid: str) -> list:
    """`share_gifts` — the off-book transfer, in or out. THE ROW THIS FEATURE IS FOR.

    Two dates, both labelled, neither substituted for the other:

      * `created_at` is when the ROW was written. It is always known.
      * the EVENT date is when the payment happened, and this table has no column for
        it. `_event_date_from_note` reads it out of the note when the note states one,
        and reports the literal substring it read. When it does not, `event_at` is None
        and the page says **unknown** — it does not quietly print `created_at` under the
        event date's label.

    Both sides of the transfer get a row, and the two rows agree on every fact while
    reading from each side: "from X" and "to Y", `+shares` and `−shares`. The direction
    is the whole meaning of the event and rendering it identically to both parties would
    make one of the two copies false.
    """
    uid = str(uid)
    rows = _rows("SELECT key, market_id, from_user, to_user, shares, basis, value_coins, "
                 "       note, created_at FROM share_gifts "
                 " WHERE from_user = ? OR to_user = ? "
                 " ORDER BY created_at DESC LIMIT ?", (uid, uid, SOURCE_CAP))
    out = []
    for r in rows:
        rec = _ts(r["created_at"])
        ev, raw, how = _event_date_from_note(r["note"])
        incoming = str(r["to_user"]) == uid
        other = counterparty(r["from_user"] if incoming else r["to_user"])
        mid = str(r["market_id"] or "")
        shares = float(r["shares"] or 0)
        head = (f"OTC transfer from {other['name']}" if incoming
                else f"OTC transfer to {other['name']}")
        det = (f"{'+' if incoming else '−'}{sh(shares)} shares · {market_name(mid)} "
               f"({ticker(mid)}) · value {c(r['value_coins'])} c as recorded")
        out.append({
            "source": "otc", "eid": str(r["key"]), "kind": _KINDS["otc"],
            "event_at": ev,
            "event_src": (f"stated in the note as “{raw}”, {how}" if raw else ""),
            "recorded_at": rec,
            "market_id": mid, "coin_delta": None,
            "headline": head, "detail_text": det, "note": str(r["note"] or ""),
            "incoming": incoming, "other": other, "shares": shares,
            "value_coins": float(r["value_coins"] or 0), "basis": float(r["basis"] or 0),
        })
    return out


def read_dividends(uid: str) -> list:
    """Dividends declared on a market this user holds shares in — joined through the
    holding, because `stock_dividend_log` has no `user_id`.

    TWO HONESTY CONSTRAINTS, both stated on the row:

    1. **The per-holder amount is not recorded anywhere.** The table stores `per_share`,
       `total_paid` and a holder count. `per_share × your shares` would be a plausible
       number and this page does not print it, because the shares that mattered are the
       shares you held on the day it paid and nothing in this schema records those. The
       coins you actually received are a `coin_ledger` row and they are on this page
       already, as a wallet movement, which is where a real figure belongs.
    2. **The holding is read as it is TODAY.** A player who has since sold out does not
       see a dividend they were paid, and a player who bought in yesterday sees one from
       before they held. There is no historical holdings table to do better with, and
       the row says which way it can be wrong rather than implying it cannot be.

    Zero rows in production. This is designed for the empty case and tested against it.
    """
    rows = _rows(
        "SELECT d.id, d.market_id, d.month, d.total_paid, d.per_share, d.holders, d.paid_at "
        "  FROM stock_dividend_log d "
        "  JOIN stock_holdings h ON h.market_id = d.market_id "
        " WHERE h.user_id = ? AND h.shares > 0 "
        " ORDER BY d.id DESC LIMIT ?", (str(uid), SOURCE_CAP))
    out = []
    for r in rows:
        at = _ts(r["paid_at"])
        mid = str(r["market_id"] or "")
        out.append({
            "source": "dividend", "eid": str(int(r["id"])), "kind": _KINDS["dividend"],
            "event_at": at, "event_src": "", "recorded_at": at,
            "market_id": mid, "coin_delta": None,
            "headline": f"Dividend declared · {market_name(mid)}",
            "detail_text": (f"{c(r['per_share'])} c per share for {_month_name(r['month'])} · "
                            f"{c(r['total_paid'])} c paid across {int(r['holders'] or 0)} holders "
                            f"· your own share is not recorded in this table"),
            "note": "", "per_share": float(r["per_share"] or 0),
            "total_paid": float(r["total_paid"] or 0), "holders": int(r["holders"] or 0),
            "month": str(r["month"] or ""),
        })
    return out


def read_investor_payouts(uid: str) -> list:
    """`investor_payout_log` — a profit share paid to this investor. Per-user and
    unambiguous: it has a `user_id` and an `amount`, so it is shown as a figure.

    It is NOT added to the coins-in total. Whether a payout also wrote a `coin_ledger`
    row is a property of the bot code that wrote it and not of this table, and a total
    that might double-count is worse than a total that says what it counts. Zero rows in
    production.
    """
    rows = _rows("SELECT id, amount, note, paid_at FROM investor_payout_log "
                 " WHERE user_id = ? ORDER BY id DESC LIMIT ?", (str(uid), SOURCE_CAP))
    out = []
    for r in rows:
        at = _ts(r["paid_at"])
        out.append({
            "source": "investor_payout", "eid": str(int(r["id"])),
            "kind": _KINDS["investor_payout"],
            "event_at": at, "event_src": "", "recorded_at": at,
            "market_id": "", "coin_delta": None,
            "headline": "Investor profit share",
            "detail_text": f"{c(r['amount'])} c paid to you as an investor",
            "note": str(r["note"] or ""), "amount": float(r["amount"] or 0),
        })
    return out


def _hive_markets(uid: str) -> dict:
    """`{market_id: role}` for markets this user owns or has harvested for.

    Owner comes from `markets.owner_id`; harvester from `hive_harvests.user_id`, which
    the bot resolves from `ign_registry` at ingest and leaves NULL when the IGN is not
    linked. An unlinked harvester therefore sees no hive rows — correct, because we
    cannot prove the row is theirs, and inventing the link from a name match would be
    guessing at identity on a money page.
    """
    uid = str(uid)
    roles: dict = {}
    for mid, m in _markets().items():
        if m.get("owner_id") and str(m["owner_id"]) == uid:
            roles[mid] = "owner"
    try:
        for r in _rows("SELECT DISTINCT market_id FROM hive_harvests WHERE user_id = ?", (uid,)):
            mid = str(r["market_id"])
            roles[mid] = "owner and harvester" if roles.get(mid) == "owner" else "harvester"
    except Exception:
        log.warning("[history] harvester lookup failed", exc_info=True)
    return roles


def read_hive(uid: str) -> list:
    """`hive_ledger` — one row per market per month. A MARKET TOTAL, never yours.

    This table has no `user_id`. `harvester_pay` is the total paid to every harvester of
    that market that month, so for a market with more than one harvester it is not any
    individual's earnings, and the file's rule forbids showing it as if it were. The
    viewer's own hive coins are `coin_ledger` rows (`hive:<market>:<report id>`) and
    they are on this page, above, as wallet movements with real per-user figures.

    So the row is labelled HIVE MONTH, every figure carries the word "market", and the
    role that earned the viewer the right to see it is printed on the row. It is
    excluded from the coins totals for the same reason it is labelled: it is not the
    viewer's money moving.

    `updated_at` is a RECORDED date — the last time the bot touched the row — and the
    month is a period. Neither is an event date, and the row does not pretend the two
    are the same thing.
    """
    roles = _hive_markets(uid)
    if not roles:
        return []
    marks = list(roles.keys())
    qs = ",".join("?" for _ in marks)
    rows = _rows(f"SELECT market_id, month, value, harvester_pay, owner_pay, net, updated_at "
                 f"  FROM hive_ledger WHERE market_id IN ({qs}) "
                 f" ORDER BY month DESC LIMIT ?", (*marks, SOURCE_CAP))
    out = []
    for r in rows:
        mid = str(r["market_id"])
        rec = _ts(r["updated_at"])
        role = roles.get(mid, "")
        out.append({
            "source": "hive", "eid": f"{mid}:{r['month']}", "kind": _KINDS["hive"],
            # The month is the period this describes; the day within it is not recorded,
            # so there is no event date to claim.
            "event_at": None,
            "event_src": f"covers {_month_name(r['month'])} · no exact date is recorded",
            "recorded_at": rec,
            "market_id": mid, "coin_delta": None,
            "headline": f"Hive month · {market_name(mid)} · {_month_name(r['month'])}",
            "detail_text": (f"market produced {c(r['value'])} c · "
                            f"{c(r['harvester_pay'])} c to all harvesters · "
                            f"{c(r['owner_pay'])} c to the owner · net {c(r['net'])} c "
                            f"— market totals, not your own figures (you are the {role})"),
            "note": "", "role": role, "month": str(r["month"] or ""),
            "value": float(r["value"] or 0), "harvester_pay": float(r["harvester_pay"] or 0),
            "owner_pay": float(r["owner_pay"] or 0), "net": float(r["net"] or 0),
        })
    return out


_READERS = {
    "coin_ledger": read_coin_ledger,
    "stock_trade": read_stock_trades,
    "otc": read_otc,
    "dividend": read_dividends,
    "investor_payout": read_investor_payouts,
    "hive": read_hive,
}


# ══════════════════════════════════════════════════════════════════════════
# Merge, filter, paginate
# ══════════════════════════════════════════════════════════════════════════

def _sort_key(e: dict) -> tuple:
    """Reverse-chronological, on the best date the event HAS.

    An event with no known event date is placed by its recorded date, and — this is the
    part that matters — the ROW SAYS SO, in `_when_cell`. Ordering has to put every row
    somewhere; the honesty rule is not that unknown dates must be unsortable, it is that
    a date used for ordering must never be printed under the label of a date we do not
    have.
    """
    at = e.get("event_at")
    if at is None:
        at = e.get("recorded_at")
    return (-(at if at is not None else 0.0), str(e.get("source")), str(e.get("eid")))


def gather(uid: str) -> tuple:
    """`(events, problems)` — every source, merged, newest first.

    `problems` is a list of plain sentences, and `_page_body` prints all of them. Two
    kinds go in it, and both are things a history page must never do quietly:

      * **a source that failed to read.** Reported by name. A history page silently
        missing a table is precisely the failure this feature exists to end; "we could
        not read your exchange fills" is a worse page and a better record.
      * **a source that hit `SOURCE_CAP`.** The page is then a PREFIX of the history and
        it says so. A truncated list presented as a complete one is a false record even
        though every row in it is true.
    """
    events: list = []
    problems: list = []
    for name, fn in _READERS.items():
        try:
            got = fn(uid)
        except Exception:
            log.exception("[history] source %s failed for %s", name, uid)
            problems.append(f"your {name} records could not be read just now, so none "
                            f"of them are below")
            continue
        if len(got) >= SOURCE_CAP:
            problems.append(f"you have more than {SOURCE_CAP:,} {name} records and only "
                            f"the most recent {SOURCE_CAP:,} are below")
        events.extend(got)
    events.sort(key=_sort_key)
    return events, problems


def apply_filters(events: list, type_f: str, market_f: str) -> list:
    """Filter by type and by market. Unknown values fall back to "everything" rather
    than to an empty page — an unrecognised query string is a typo or a stale link, and
    a blank history is the most alarming thing this page can show somebody."""
    out = events
    if type_f and type_f != "all" and type_f in _READERS:
        out = [e for e in out if e["source"] == type_f]
    if market_f:
        out = [e for e in out if str(e.get("market_id") or "") == market_f]
    return out


def totals(events: list) -> dict:
    """Coins in / coins out / net, over the events passed in.

    ONLY `coin_ledger` rows count. They are the only wallet movements in the six
    sources; a dividend declaration and a hive month are events *about* money, and the
    coins from them are already in `coin_ledger` under their own row. Summing both would
    double-count, and a total that might double-count is worse than no total.

    `counted` and the date span are returned WITH the figures so the strip can print
    what it counted and over what period. UI_PRINCIPLES part 3: a figure with no unit
    and no timeframe is a bug, and "net 41,204" with neither is the exact bug.
    """
    ins = outs = 0.0
    counted = 0
    stamps = []
    for e in events:
        d = e.get("coin_delta")
        if d is None:
            continue
        counted += 1
        if d >= 0:
            ins += d
        else:
            outs += -d
        at = e.get("event_at") or e.get("recorded_at")
        if at is not None:
            stamps.append(at)
    return {"in": ins, "out": outs, "net": ins - outs, "counted": counted,
            "other": len(events) - counted,
            "first": min(stamps) if stamps else None,
            "last": max(stamps) if stamps else None}


def markets_present(events: list) -> list:
    """`[(market_id, name)]` for the markets that actually appear in this user's
    history, most-used first. The filter offers the markets a person has touched, not a
    directory of every market on the server."""
    seen: dict = {}
    for e in events:
        mid = str(e.get("market_id") or "")
        if mid:
            seen[mid] = seen.get(mid, 0) + 1
    return [(mid, market_name(mid)) for mid, _n in
            sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))]


def may_view(uid: str, e: dict) -> bool:
    """Is `uid` a party to this event?

    Every event in `gather(uid)` was fetched by a query already scoped to `uid`, so this
    is not the gate for the timeline — it is the gate for the PERMALINK, which is
    reached by an id and not by a user. It is implemented as a re-derivation rather than
    a second rule: fetch what this viewer can see from the source, and look for the id.
    One rule, one implementation, no way for the two surfaces to disagree about who may
    see what.
    """
    fn = _READERS.get(str(e.get("source")))
    if fn is None:
        return False
    try:
        return any(str(x["eid"]) == str(e["eid"]) for x in fn(uid))
    except Exception:
        log.exception("[history] permalink authorisation read failed")
        return False


def find_event(uid: str, source: str, eid: str) -> Optional[dict]:
    """The one event, IF this viewer is a party to it, else None.

    Same shape as `messages_web._participant_thread`: authorisation is a property of the
    row, the lookup is scoped to the viewer, and a stranger's request is indistinguishable
    from a request for an id that does not exist. Both are 404.
    """
    fn = _READERS.get(str(source))
    if fn is None:
        return None
    try:
        for e in fn(str(uid)):
            if str(e["eid"]) == str(eid):
                return e
    except Exception:
        log.exception("[history] permalink lookup failed")
    return None


# ══════════════════════════════════════════════════════════════════════════
# Rendering
# ══════════════════════════════════════════════════════════════════════════

#: Section-local CSS. Zero border-radius, no emoji, mono tabular figures, two type
#: colours plus green/red for direction. Nothing here overrides a shell token.
_CSS = """
<style>
.h-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1px;
  background:var(--border);border:1px solid var(--border);margin-bottom:14px}
.h-seg{background:var(--surface);padding:12px 14px}
.h-lab{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)}
.h-val{font-family:var(--font-data);font-variant-numeric:tabular-nums slashed-zero;
  font-size:18px;font-weight:600;margin-top:3px;letter-spacing:-.01em}
.h-sub{font-size:10.5px;color:var(--faint);margin-top:3px;line-height:1.45}
.h-filters{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
.h-chip{padding:5px 10px;border:1px solid var(--border);background:var(--panel2);
  font-size:11px;color:var(--text-body);font-family:var(--font-ui)}
.h-chip:hover{border-color:var(--border-strong);color:var(--text)}
.h-chip.on{border-color:var(--accent);color:var(--accent)}
.h-when{white-space:nowrap;font-family:var(--font-data);font-variant-numeric:tabular-nums}
.h-kind{font-family:var(--font-data);font-size:9.5px;letter-spacing:.11em;color:var(--muted);
  white-space:nowrap}
.h-kind.otc{color:var(--amber)}
.h-head{font-size:12.5px;color:var(--text)}
.h-det{font-size:11px;color:var(--text-body);margin-top:2px;line-height:1.5}
.h-prov{font-size:10.5px;color:var(--faint);margin-top:2px;line-height:1.5}
.h-note{font-size:11px;color:var(--text-body);margin-top:3px;border-left:2px solid var(--border-strong);
  padding-left:8px;white-space:pre-wrap;overflow-wrap:anywhere}
.h-unk{color:var(--amber)}
.h-empty{padding:16px 0;color:var(--muted);font-size:12px}
.h-page{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:12px;
  font-size:11px;color:var(--muted);font-family:var(--font-data)}
.h-drift{color:var(--amber);font-size:10.5px}
.h-kv{display:grid;grid-template-columns:190px 1fr;gap:8px 16px;font-size:12.5px}
.h-kv dt{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  padding-top:2px}
.h-kv dd{margin:0;overflow-wrap:anywhere}
</style>
"""


def _when_cell(e: dict) -> str:
    """The date column. ONE date, correctly labelled, and never a substitution.

    Three shapes, and the difference between them is the point of this file:

      * event date known         -> the date, plain.
      * event date unknown, but a write stamp exists -> the word **unknown** in amber,
        and underneath, in words, that the row is POSITIONED by when the bot wrote it.
        The write stamp itself is NOT printed — it is not a date about the event.
      * neither                  -> unknown, and "no date recorded".
    """
    ev, rec = e.get("event_at"), e.get("recorded_at")
    if ev is not None:
        return (f'<div class="h-when" title="{esc(_stamp(ev))}">{esc(_date(ev))}</div>')
    if rec is not None:
        return ('<div class="h-when h-unk">unknown</div>'
                '<div class="h-sub">no event date recorded<br>'
                'placed by when the bot wrote the row</div>')
    return '<div class="h-when h-unk">unknown</div><div class="h-sub">no date recorded</div>'


def _amount_cell(e: dict) -> str:
    """The figure, with its unit, coloured only where direction is a fact.

    `coin_ledger` moves the wallet, so its delta is green or red. Everything else prints
    its own figure with its own unit and no direction colour, because "positive" is not
    a property a dividend declaration or a hive month has for the viewer.
    """
    d = e.get("coin_delta")
    if d is not None:
        cls = "up" if d >= 0 else "down"
        return f'<span class="{cls}">{esc(c(d, sign=True))} c</span>'
    if e["source"] == "otc":
        sign = "+" if e.get("incoming") else "−"
        cls = "up" if e.get("incoming") else "down"
        return (f'<span class="{cls}">{sign}{esc(sh(e.get("shares")))}</span>'
                f'<div class="h-sub">shares · {esc(ticker(e.get("market_id")))}</div>')
    if e["source"] == "stock_trade":
        return (f'{esc(sh(e.get("shares")))}<div class="h-sub">shares'
                + (f' · {esc(c(e.get("total")))} c' if e.get("total") else '')
                + '</div>')
    if e["source"] == "investor_payout":
        return f'<span class="up">{esc(c(e.get("amount"), sign=True))} c</span>'
    if e["source"] == "dividend":
        return (f'{esc(c(e.get("per_share")))}<div class="h-sub">c per share</div>')
    if e["source"] == "hive":
        return (f'{esc(c(e.get("net")))}<div class="h-sub">c net · market</div>')
    return "—"


def _balance_cell(e: dict) -> str:
    """The running balance — STORED, not recomputed, and honest when they disagree.

    Only `coin_ledger` has one. Where the stored figure disagrees with
    `previous.balance_after + delta`, the stored figure is shown and the disagreement is
    printed next to it. 23 of 117 checkable production rows disagree, which is a real
    property of a ledger that has had movements written outside it, and hiding it would
    make this page a worse record than the table it reads.
    """
    if e["source"] != "coin_ledger":
        return '<span class="muted">—</span>'
    out = f'{esc(c(e.get("balance_after")))} c'
    drift = e.get("balance_drift")
    if drift is not None:
        out += (f'<div class="h-drift" title="stored {c(e.get("balance_after"))} c · '
                f'recomputed {c(drift)} c">stored · a recomputation from the row above '
                f'gives {esc(c(drift))} c</div>')
    return out


def _detail_cell(e: dict, permalink: bool = False) -> str:
    """Headline, detail, provenance, note. THE SECOND LINE IS WHERE THE HONESTY LIVES.

    For the OTC row it carries where the event date was read from, and the note verbatim;
    for a wallet movement it carries the raw `reason` in the tooltip so the humanised
    headline can be checked. It does NOT carry `recorded_at` — see the header.
    """
    parts = [f'<div class="h-head">{esc(e["headline"])}</div>']
    if e.get("detail_text"):
        parts.append(f'<div class="h-det">{esc(e["detail_text"])}</div>')

    prov = []
    if e.get("event_src"):
        prov.append(f'event date {esc(e["event_src"])}')
    elif e.get("event_at") is None and e.get("recorded_at") is not None:
        prov.append("no event date is recorded for this event")
    if prov:
        parts.append(f'<div class="h-prov">{" · ".join(prov)}</div>')

    if e.get("note"):
        note = esc(e["note"]) if permalink else esc(_clip(e["note"], 160))
        parts.append(f'<div class="h-note">{note}</div>')
    return "".join(parts)


def _clip(s: Any, limit: int) -> str:
    t = " ".join(str(s or "").split())
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def _kind_cell(e: dict) -> str:
    cls = " otc" if e["source"] == "otc" else ""
    return f'<span class="h-kind{cls}">{esc(e["kind"])}</span>'


def _permalink(e: dict) -> str:
    """`/history/e/{source}/{eid}`. The id is percent-encoded because a `share_gifts`
    key is a bot-built string (`gift:<market>:<from>:<to>:<shares>`) and not a number —
    colons survive a path segment, but a stray `?` or `#` in a future key would silently
    truncate the address. aiohttp decodes it back before `h_event` sees it."""
    from urllib.parse import quote
    return (f'/history/e/{quote(str(e["source"]), safe="")}/'
            f'{quote(str(e["eid"]), safe=":")}')


def _row_html(e: dict) -> str:
    link = esc(_permalink(e))
    return (f'<tr>'
            f'<td>{_when_cell(e)}</td>'
            f'<td>{_kind_cell(e)}</td>'
            f'<td style="text-align:left">{_detail_cell(e)}</td>'
            f'<td>{_amount_cell(e)}</td>'
            f'<td>{_balance_cell(e)}</td>'
            f'<td><a class="btn ghost sm" href="{link}">Link</a></td>'
            f'</tr>')


def _qs(type_f: str, market_f: str, page: int) -> str:
    bits = []
    if type_f and type_f != "all":
        bits.append(f"type={type_f}")
    if market_f:
        bits.append(f"market={market_f}")
    if page > 1:
        bits.append(f"page={page}")
    return ("?" + "&amp;".join(bits)) if bits else ""


def _filters_html(events: list, type_f: str, market_f: str) -> str:
    """Filter chips. Plain links, no JavaScript — the whole page is server-rendered and
    a filter that needs a script to work is a filter that fails for the one person
    reading this page with something unusual."""
    chips = []
    for key, label in FILTER_TYPES:
        n = len(events) if key == "all" else sum(1 for e in events if e["source"] == key)
        on = " on" if (type_f or "all") == key else ""
        chips.append(f'<a class="h-chip{on}" href="/history{_qs(key, market_f, 1)}">'
                     f'{esc(label)} <span class="muted">{n}</span></a>')
    row1 = f'<div class="h-filters">{"".join(chips)}</div>'

    mks = markets_present(events)
    if not mks:
        return row1
    mchips = [f'<a class="h-chip{"" if market_f else " on"}" '
              f'href="/history{_qs(type_f, "", 1)}">All markets</a>']
    for mid, name in mks:
        on = " on" if market_f == mid else ""
        mchips.append(f'<a class="h-chip{on}" href="/history{_qs(type_f, mid, 1)}">'
                      f'{esc(name)}</a>')
    return row1 + f'<div class="h-filters">{"".join(mchips)}</div>'


def _totals_html(t: dict, type_f: str, market_f: str) -> str:
    """The totals strip. EVERY FIGURE CARRIES ITS UNIT AND ITS TIMEFRAME.

    Coins in, coins out, net — and a fourth segment that says what was counted and what
    was not, because three coin figures over a filtered range with no statement of scope
    is three numbers a person can only guess at. In / out / net are also never collapsed
    into one "balance": they are different things, which is the house rule money pages
    are held to.
    """
    span = ("no dated wallet movements in this view" if t["first"] is None
            else (f"{_date(t['first'])} to {_date(t['last'])}"
                  if t["first"] != t["last"] else _date(t["first"])))
    scope = []
    if type_f and type_f != "all":
        scope.append(dict(FILTER_TYPES).get(type_f, type_f))
    if market_f:
        scope.append(market_name(market_f))
    scope_s = " · ".join(scope) if scope else "the whole history"

    return f"""
<div class="h-strip">
  <div class="h-seg"><div class="h-lab">Coins in</div>
    <div class="h-val up">{esc(c(t['in']))} c</div>
    <div class="h-sub">{esc(span)}</div></div>
  <div class="h-seg"><div class="h-lab">Coins out</div>
    <div class="h-val down">{esc(c(t['out']))} c</div>
    <div class="h-sub">{esc(span)}</div></div>
  <div class="h-seg"><div class="h-lab">Net</div>
    <div class="h-val">{esc(c(t['net'], sign=True))} c</div>
    <div class="h-sub">in minus out · {esc(span)}</div></div>
  <div class="h-seg"><div class="h-lab">What this counts</div>
    <div class="h-val">{esc(f"{t['counted']:,}")}</div>
    <div class="h-sub">{esc("wallet movement" if t["counted"] == 1 else "wallet movements")},
      over {esc(scope_s)}. {esc(f"{t['other']:,}")}
      {esc("other event shown below is" if t["other"] == 1 else "other events shown below are")}
      not coin movements and not in these figures.</div></div>
</div>"""


def _page_body(uid: str, events_all: list, events: list, page: int, pages: int,
               type_f: str, market_f: str, problems: list) -> str:
    rows = events[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    if rows:
        table = ('<div class="tablewrap"><table><thead><tr>'
                 '<th>Date</th><th>Type</th><th style="text-align:left">What happened</th>'
                 '<th>Amount</th><th>Balance after</th><th></th>'
                 '</tr></thead><tbody>'
                 + "".join(_row_html(e) for e in rows) + '</tbody></table></div>')
    elif events_all:
        table = ('<div class="h-empty">Nothing in your history matches this filter.</div>')
    else:
        # THE EMPTY STATE. One muted line — no illustration, no suggestion, no example
        # row. Most players have no history at all, and a page that fills that space
        # with something decorative is a page that has started inventing.
        table = ('<div class="h-empty">Nothing on record for you yet. This page shows '
                 'what the bot has already written down; it does not create anything.</div>')

    nav = ""
    if pages > 1:
        prev = (f'<a class="btn ghost sm" href="/history{_qs(type_f, market_f, page - 1)}">'
                f'Newer</a>' if page > 1 else '<span></span>')
        nxt = (f'<a class="btn ghost sm" href="/history{_qs(type_f, market_f, page + 1)}">'
               f'Older</a>' if page < pages else '<span></span>')
        nav = (f'<div class="h-page">{prev}<span>Page {page} of {pages} · '
               f'{len(events):,} events in this view</span>{nxt}</div>')

    warn = ""
    if problems:
        warn = ('<div class="notebox">This page is not showing you everything: '
                + esc("; ".join(problems)) + '. Every other source below is complete, '
                'and nothing has been left out of it silently.</div>')

    return f"""{_CSS}
<div class="page-head">
  <div>
    <h1>History</h1>
    <div class="page-sub">Every transaction this site has a record of, for your account,
    newest first. Each row is read from what the bot already wrote down and is labelled
    for what it actually was — a transfer made outside the exchange says so, and each
    date is the date of the event itself, with where it was read from printed under
    it.</div>
  </div>
</div>
{warn}
{_totals_html(totals(events), type_f, market_f)}
{_filters_html(events_all, type_f, market_f)}
{table}
{nav}
"""


def _event_page_body(uid: str, e: dict) -> str:
    """The permalink page. One event, every stored field, nothing added.

    This is the page John hands to somebody. So it prints the raw stored values next to
    the rendered ones — the exact `note`, the exact `reason`, the counterparty's full
    Discord id, the table and row id — because "here is our record" is only worth
    anything if the person reading it can see what the record literally says.
    """
    other = e.get("other") or {}
    kv = []

    def add(label: str, value: str) -> None:
        kv.append(f"<dt>{esc(label)}</dt><dd>{value}</dd>")

    add("Event", esc(e["kind"]) + " · " + esc(e["headline"]))
    if e.get("event_at") is not None:
        add("Event date", f'{esc(_stamp(e["event_at"]))}'
                          + (f'<div class="h-prov">{esc(e["event_src"])}</div>'
                             if e.get("event_src") else ""))
    else:
        add("Event date", '<span class="h-unk">unknown</span>'
                          '<div class="h-prov">no event date is recorded for this '
                          'event.</div>'
            + (f'<div class="h-prov">{esc(e["event_src"])}</div>'
               if e.get("event_src") else ""))
    if e.get("detail_text"):
        add("Detail", esc(e["detail_text"]))
    if other:
        add("Counterparty",
            esc(other.get("name", "")) +
            (f'<div class="h-prov">{esc(other.get("sub"))}</div>' if other.get("sub") else "") +
            (f'<div class="h-prov">Discord id {esc(other.get("id"))}</div>'
             if other.get("id") else ""))
    if e.get("market_id"):
        add("Market", f'{esc(market_name(e["market_id"]))} '
                      f'<span class="muted">({esc(ticker(e["market_id"]))} · '
                      f'{esc(e["market_id"])})</span>')
    if e.get("coin_delta") is not None:
        add("Wallet movement", f'<span class="{"up" if e["coin_delta"] >= 0 else "down"}">'
                               f'{esc(c(e["coin_delta"], sign=True))} c</span>')
        add("Balance after", _balance_cell(e))
    if e["source"] == "otc":
        add("Shares", ("+" if e.get("incoming") else "−") + esc(sh(e.get("shares")))
            + " " + esc(market_name(e.get("market_id"))))
        add("Value recorded", esc(c(e.get("value_coins"))) + " c"
            + '<div class="h-prov">the value written with the transfer. It is not a '
              'payment receipt — this site holds no record of coins moving for it, '
              'because they did not move here.</div>')
        add("Cost basis carried", esc(c(e.get("basis"))) + " c")
    if e.get("note"):
        add("Note, as stored", f'<div class="h-note">{esc(e["note"])}</div>')
    if e["source"] == "coin_ledger":
        add("Reason, as stored",
            (f'<span class="mono">{esc(e["note"])}</span>' if e.get("note")
             else '<span class="muted">empty — the bot wrote no reason</span>'))
    add("Source", f'<span class="mono">{esc(e["source"])}</span> row '
                  f'<span class="mono">{esc(e["eid"])}</span>')

    return f"""{_CSS}
<div class="page-head">
  <div>
    <h1>{esc(e["kind"])}</h1>
    <div class="page-sub">One event, as recorded. Both parties to it can open this page
    and see the same thing; nobody else can. Nothing on it is calculated — every value
    below is what the database holds.</div>
  </div>
  <a class="btn ghost sm" href="/history">All history</a>
</div>
<div class="tile s12"><dl class="h-kv">{"".join(kv)}</dl></div>
"""


_PAGE_JS = "loadMe().then(() => { renderStrip(); });"


# ══════════════════════════════════════════════════════════════════════════
# Routes — two, both GET, both read-only
# ══════════════════════════════════════════════════════════════════════════

def _int_arg(raw: Any, default: int, lo: int, hi: int) -> int:
    """A query-string integer, clamped. Junk becomes the default rather than a 500."""
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


async def h_history(request):
    """`GET /history` — the timeline. Logged out gets 401 and the sign-in card.

    Identity is `shell.require_page_session` and nothing else. There is no user id in
    the query string this handler reads, and no branch of it can render one user's rows
    to another: every source query is scoped by `uid` at the SQL level.
    """
    sess, refusal = shell.require_page_session(request)
    if refusal is not None:
        return refusal
    uid = str(sess["user_id"])

    events_all, problems = gather(uid)
    type_f = str(request.query.get("type") or "all").strip().lower()
    if type_f not in _READERS and type_f != "all":
        type_f = "all"
    market_f = str(request.query.get("market") or "").strip().lower()
    if market_f and market_f not in {m for m, _ in markets_present(events_all)}:
        market_f = ""

    events = apply_filters(events_all, type_f, market_f)
    pages = max(1, (len(events) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = _int_arg(request.query.get("page"), 1, 1, pages)

    body = _page_body(uid, events_all, events, page, pages, type_f, market_f, problems)
    return shell.page("History", "history", body, _PAGE_JS)


async def h_event(request):
    """`GET /history/e/{source}/{eid}` — one event, for the people it happened to.

    A stranger and a nonexistent id get the SAME 404 page. `find_event` reads the source
    scoped to the viewer and looks for the id in what came back, so a row this viewer is
    not a party to is never fetched, never rendered, and never distinguishable from one
    that does not exist. The ids are guessable; that does not matter, because the
    address is not the authorisation.
    """
    sess, refusal = shell.require_page_session(request)
    if refusal is not None:
        return refusal
    uid = str(sess["user_id"])
    source = str(request.match_info.get("source") or "")
    eid = str(request.match_info.get("eid") or "")

    e = find_event(uid, source, eid)
    if e is None:
        body = (_CSS + '<div class="page-head"><div><h1>Transaction</h1>'
                '<div class="page-sub">No such record.</div></div>'
                '<a class="btn ghost sm" href="/history">All history</a></div>'
                '<div class="h-empty">This record does not exist, or you were not a '
                'party to it.</div>')
        resp = shell.page("History", "history", body, _PAGE_JS)
        resp.set_status(404)
        return resp
    return shell.page(f"{e['kind']} · History", "history", _event_page_body(uid, e),
                      _PAGE_JS)


# ══════════════════════════════════════════════════════════════════════════
# Mount
# ══════════════════════════════════════════════════════════════════════════

#: Lucide-style receipt/list icon, inline SVG, stroke 1.7. THEME.md rule 7: no emoji.
_ICON = ('<path d="M5 3h14v18l-3-2-2 2-2-2-2 2-3-2z"/>'
         '<path d="M9 8h6M9 12h6M9 16h3"/>')


def _register_with_hub(key: str, label: str, path: str, order: int) -> None:
    """Tell `hub_web` this section exists, so the hub nav lists it. Same guarded shape
    as `messages_web` — a section nothing links to is not shipped."""
    try:
        import hub_web
    except Exception:
        return
    try:
        hub_web.register_section(key, label, path, icon=_ICON, order=order)
    except Exception as e:  # pragma: no cover
        log.warning("[%s] could not register with the hub nav: %s", key, e)


def register_history_routes(app) -> None:
    """Attach the history section. Mirrors `messages_web.register_messages_routes`."""
    if web is None:  # pragma: no cover
        log.warning("[history] aiohttp unavailable — history not registered.")
        return
    shell.register_shell_routes(app)
    _register_with_hub("history", "History", "/history", order=60)
    app.router.add_get("/history", h_history)
    app.router.add_get("/history/e/{source}/{eid}", h_event)
    log.info("[history] v%s registered (timeline · permalink) — read-only",
             HISTORY_VERSION)
