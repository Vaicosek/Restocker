"""
messages_web.py — one-to-one messaging between V Tech site users.

WHAT THIS IS, AND WHAT IT IS NOT
────────────────────────────────
This is a NEW feature. It was never built and then lost: all three database
snapshots carry 59 tables and none of them holds a message, and `Restocker_web.py`
has never routed one. The thing that exists and works is `cogs/land_exchange.py`'s
**deal room**, which opens a *Discord* channel between a buyer and a seller after a
land sale. That is a different system on a different surface, and this file does not
touch it.

Because it is new, it **ships empty**. Nothing here seeds, backfills or demonstrates a
conversation. The entire value of a message log is that every line in it was typed by
a player, so a single invented line would poison the thing it is meant to prove. The
empty state is one muted sentence that says so.

THE FIVE DECISIONS, MADE ON PURPOSE
───────────────────────────────────
1. **Who may message whom — counterparties and staff, not "anyone".**
   `_may_message`. You may open a thread with a player you have actually dealt with
   (they bid on your lot, or you bid on theirs), with anyone you already have a thread
   with, and with staff; staff may reach any player with a wallet. This is a fifteen-
   holder closed economy with partner servers attached, and an open directory in it is
   not a feature, it is a way to reach every holder of every coin from one form. The
   relationship rule is also the block list — see decision 5.

2. **A recipient who has never signed in to the website still receives.** Delivery does
   not consult the recipient's login state at all — this module never reads a session
   that is not the caller's own, so it cannot. The message is written and waits. The
   alternative, refusing to send to somebody who has not signed in, would make the send
   form an oracle for who has an account and would fail exactly for the counterparty you
   most need to reach after a sale.
   The one thing the sender IS told, inside their own thread, is when the other side has
   no wallet in this economy at all (`_has_wallet`) — because that is the difference
   between "waiting for them to look" and "typing into a void". It is a disclosure, it
   is limited to a counterparty who already knows who they dealt with, and it is made
   deliberately rather than by accident. Login state itself is never disclosed, because
   it is never read.

3. **Append-only. No edit, no delete, no retract, for anybody, including staff.**
   Not a soft-delete, not a tombstone: there is no code path in this file that UPDATEs
   or DELETEs a row in `vt_messages`. John's reason for wanting this is that it is a
   record he can rely on later, and a record with an eraser in it is a record with an
   argument in it. If retraction is ever wanted it must arrive as an *appended* event
   with both sides seeing it, not as a mutation — and it does not arrive today, because
   nothing calls it.

4. **No read receipts.** Read state exists — `vt_message_reads` — because the unread
   count needs a watermark, but it is private to its owner and is never rendered to the
   other side. A receipt would be a lie as often as not (a background poll marks read,
   a person does not) and it converts a message into an obligation.

5. **Rate limiting: per SENDER, not per IP.** `Restocker_web`'s 120-req/60s throttle is
   per-IP and exempts `/api/bank/`; messaging is NOT exempted from it, and it is not
   enough on its own — a shared partner-server NAT puts many players behind one IP, and
   one player behind a phone tether gets a whole budget to themselves. So sends are
   capped at `SEND_PER_MINUTE` per user_id per rolling 60s and one NEW thread per
   `NEW_THREAD_COOLDOWN` seconds, both counted by SELECTing the message table rather
   than by keeping a counter. A refusal here is a `NoEffect`: it provably wrote nothing,
   so the form key is handed back and the corrected send works.

THE COUNTER THAT IS NOT A COUNTER
─────────────────────────────────
Unread is **derived, never incremented**. Each participant stores one watermark
(`last_read_message_id`) and the count is `COUNT(*) WHERE id > watermark AND sender <>
me`. This project has already paid for counters written from a stale read; a watermark
cannot drift, because the only write is `MAX(old, new)` inside SQL, so a stale reader
can only fail to advance it — never invent unread that is not there and never bury
unread that is.

IDENTITY
────────
`shell.session_user` on every route, and nothing else, ever. A user id in a body is
ignored and alarmed (`_note_identity` → `hub_attack_log`), exactly as the existing
routes do it.
"""

from __future__ import annotations

import html
import json
import logging
import time
from typing import Any, Optional

try:
    from aiohttp import web
except Exception:  # pragma: no cover - aiohttp absent in a bare import check
    web = None  # type: ignore

import vt_web_shell as shell

log = logging.getLogger("messages_web")

MESSAGES_VERSION = "1.0"

#: A message body longer than this is not a message, it is a payload. Counted in
#: characters after stripping, and enforced BEFORE the row is written — an unbounded
#: TEXT column is an unbounded page.
BODY_MAX = 2000

#: Thread page size. A conversation is paginated from the newest end, because that is
#: the end a person opens it at.
PAGE_SIZE = 50

#: Inbox page size. Fifteen holders will not exceed this for years; it is here so the
#: query is bounded on the day that stops being true.
INBOX_LIMIT = 100

#: Rate limits, per SENDER (see the header, decision 5).
SEND_PER_MINUTE = 20
NEW_THREAD_COOLDOWN = 10

#: The one endpoint name every send claims under. `shell.in_flight_keys` files by
#: this string, so it must be identical at mint, at claim and at re-render.
SEND_ENDPOINT = "messages/send"


# ══════════════════════════════════════════════════════════════════════════
# Lazy module handles — same convention as `estates_web`, so this file imports
# and is testable without the bot present.
# ══════════════════════════════════════════════════════════════════════════

def _core_db():
    import Restocker_db as _db
    return _db


# ══════════════════════════════════════════════════════════════════════════
# Schema
# ══════════════════════════════════════════════════════════════════════════

#: THE PAIR UNIQUENESS IS IN THE DATABASE, NOT IN PYTHON.
#:
#: `user_lo`/`user_hi` are the two participants sorted, so one pair has exactly one
#: ordering and the UNIQUE index on them makes a second thread for that pair
#: impossible — not unlikely, impossible. Two simultaneous "message this player for
#: the first time" clicks therefore produce one thread and one message, arbitrated by
#: SQLite rather than by a read-then-insert in this file. `CHECK(user_lo < user_hi)`
#: makes a self-thread unrepresentable at the same level.
#:
#: `last_message_at`/`last_message_id` are a denormalised ordering key, and they are
#: only ever moved FORWARD (`WHERE last_message_id < ?`), so a slow writer cannot drag
#: the inbox backwards. They are ordering, never truth: every count and every body is
#: read from `vt_messages`.
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS vt_message_threads (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_lo          TEXT NOT NULL,
        user_hi          TEXT NOT NULL,
        created_at       REAL NOT NULL,
        created_by       TEXT NOT NULL,
        last_message_at  REAL NOT NULL DEFAULT 0,
        last_message_id  INTEGER NOT NULL DEFAULT 0,
        CHECK (user_lo < user_hi)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_vt_thread_pair
        ON vt_message_threads(user_lo, user_hi)
    """,
    # "My threads, most recent first" — one index per side of the pair, so the UNION
    # in `_threads_for` is two index scans and no sort of the whole table.
    """
    CREATE INDEX IF NOT EXISTS ix_vt_thread_lo
        ON vt_message_threads(user_lo, last_message_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_vt_thread_hi
        ON vt_message_threads(user_hi, last_message_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS vt_messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id   INTEGER NOT NULL,
        sender_id   TEXT NOT NULL,
        body        TEXT NOT NULL,
        created_at  REAL NOT NULL,
        idem_key    TEXT
    )
    """,
    # "This thread, newest page" — and the same index answers the unread count,
    # which is a range scan of (thread_id, id > watermark).
    """
    CREATE INDEX IF NOT EXISTS ix_vt_msg_thread
        ON vt_messages(thread_id, id DESC)
    """,
    # The second lock on the double-click, one level below the form key: even a
    # request that somehow got past the claim cannot write a second row for the same
    # key. Partial, because a row predating the key convention would carry NULL and
    # SQLite treats NULLs as distinct.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_vt_msg_idem
        ON vt_messages(idem_key) WHERE idem_key IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS vt_message_reads (
        thread_id             INTEGER NOT NULL,
        user_id               TEXT NOT NULL,
        last_read_message_id  INTEGER NOT NULL DEFAULT 0,
        updated_at            REAL NOT NULL,
        PRIMARY KEY (thread_id, user_id)
    )
    """,
)

_SCHEMA_READY = False


def _ensure_tables() -> None:
    """Create the messaging tables if they are absent. Idempotent, and migrated the
    way the rest of the site migrates: `CREATE TABLE IF NOT EXISTS` executed on the
    core connection at first use, not a migration script somebody has to remember to
    run. Same shape as `vt_web_shell._ensure_idem_table`.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _core_db().db() as conn:
        for stmt in _SCHEMA:
            conn.execute(stmt)
    _SCHEMA_READY = True


# ══════════════════════════════════════════════════════════════════════════
# Small helpers
# ══════════════════════════════════════════════════════════════════════════

def _now() -> float:
    """Seconds since the epoch, UTC. Every timestamp in this module is stored as this
    and rendered by `_ago`; no local time is ever written and no ISO string is ever
    shown to a player."""
    return time.time()


def _pair(a: str, b: str) -> tuple:
    """The two participants in the table's canonical order."""
    a, b = str(a), str(b)
    return (a, b) if a < b else (b, a)


def esc(s: Any) -> str:
    """Escape AT RENDER, never at write.

    The database holds exactly the characters the player typed, so the record stays
    true and a future reader (a Discord relay, an export, a different template) is not
    handed HTML entities it has to guess how to undo. Every path that puts a body on a
    page goes through here — see `_message_html`.
    """
    return html.escape(str(s if s is not None else ""), quote=True)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _ago(ts: Any, now: Optional[float] = None) -> str:
    """`1749740000.0` -> `"3 hours ago"` or `"12 Jun 2026"`. Never an ISO string.

    Relative inside a week, because that is the window in which "when" means "how long
    ago"; absolute beyond it, because "37 days ago" is a number a person has to convert.
    """
    try:
        t = float(ts or 0)
    except (TypeError, ValueError):
        return "—"
    if t <= 0:
        return "—"
    d = (now if now is not None else _now()) - t
    if d < 0:
        d = 0
    if d < 45:
        return "just now"
    if d < 90:
        return "a minute ago"
    if d < 3600:
        return f"{int(round(d / 60))} minutes ago"
    if d < 7200:
        return "an hour ago"
    if d < 86400:
        return f"{int(d // 3600)} hours ago"
    if d < 172800:
        return "yesterday"
    if d < 604800:
        return f"{int(d // 86400)} days ago"
    tm = time.gmtime(t)
    return f"{tm.tm_mday} {_MONTHS[tm.tm_mon - 1]} {tm.tm_year}"


def _stamp(ts: Any) -> str:
    """The exact time, for a `title` tooltip — human, UTC, and still not ISO."""
    try:
        tm = time.gmtime(float(ts or 0))
    except (TypeError, ValueError):
        return ""
    return (f"{tm.tm_mday} {_MONTHS[tm.tm_mon - 1]} {tm.tm_year} "
            f"{tm.tm_hour:02d}:{tm.tm_min:02d} UTC")


_NAMES_CACHE: dict = {}
_NAMES_AT = 0.0


def _names() -> dict:
    """`{user_id: display name}` from the same `stock_names.yml` the exchange and the
    estates register read. One source of names on the site."""
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
    """The name of the OTHER side of a thread you are already in.

    This deliberately does NOT apply `estates_web._is_anonymous`. The anonymity toggle
    governs PUBLIC boards — an auction ladder, a leaderboard, a parcel register — where
    a player is listed to people who have no business with them. A one-to-one thread is
    the opposite shape: it is directed, it has two people in it, both of them chose to
    be in it or dealt with the other to get there, and it discloses no more than the
    Discord deal room already does by putting both parties in one channel. Rendering
    "Another player" on both ends of an inbox would make the inbox unusable while
    protecting nobody, since each side already knows who they dealt with.

    An id fragment is never shown: an unnamed player is "Unnamed player", because a
    truncated snowflake is still an id and it identifies nobody while looking like it
    should.
    """
    uid = str(uid or "")
    if not uid:
        return "—"
    if uid.startswith("treasury:"):
        return "V Tech treasury"
    nm = _names().get(uid)
    nm = str(nm).strip() if nm else ""
    return nm or "Unnamed player"


def _note_identity(request, body: Any, sess: dict, endpoint: str,
                   shell_log: bool = True) -> None:
    """A user id in a body is IGNORED and alarmed. Two alarms on purpose.

    `shell.note_body_identity` is what `banking_web` and `estates_web` call and it
    writes a log line; `hub_web`'s `hub_attack_log` is the durable table that exists
    for exactly this and that an operator can actually query a week later. Both are
    guarded: a failure to write the alarm must never become a failure to refuse.

    `shell_log=False` on the send path, where `shell.money_post` writes that half
    itself — one event should not produce two identical log lines and make an operator
    think there were two attempts.
    """
    if shell_log:
        try:
            shell.note_body_identity(request, body, sess)
        except Exception:  # pragma: no cover
            log.warning("[messages] shell identity alarm failed", exc_info=True)
    try:
        import hub_web
        hub_web._scan_body_identity(body if isinstance(body, dict) else {},
                                    request, str(sess.get("user_id") or ""), endpoint)
    except Exception:  # pragma: no cover - hub absent in a bare embed
        pass


# ══════════════════════════════════════════════════════════════════════════
# Who may message whom
# ══════════════════════════════════════════════════════════════════════════

def _contacts(uid: str) -> list:
    """Everybody `uid` is allowed to open a NEW thread with, most recent dealing first.

    Three sources, and each one is a real relationship rather than a directory:

      * land counterparties — they bid on a lot you listed, or you bid on a lot they
        listed. This is the same relationship `cogs/land_exchange.py` opens its Discord
        deal room on, which is the thing John remembers working.
      * anybody you already have a thread with, so a conversation stays repliable long
        after the lot that started it has settled.
      * staff, who are reachable by everyone because a support channel that only some
        players can use is not a support channel.

    Bidders against each other on the same lot are NOT here: they have no dealing, and
    an auction is anonymous on purpose.
    """
    uid = str(uid)
    _ensure_tables()
    out: dict = {}

    def add(other: str, via: str, at: float) -> None:
        other = str(other or "")
        if not other or other == uid:
            return
        cur = out.get(other)
        if cur is None or at > cur["at"]:
            out[other] = {"user_id": other, "via": via, "at": float(at or 0)}
        elif not cur.get("via"):
            cur["via"] = via

    try:
        with _core_db().db() as conn:
            rows = conn.execute(
                "SELECT b.bidder_id AS other, l.id AS lot, l.title AS title, "
                "       MAX(b.id) AS seq "
                "  FROM land_bids b JOIN land_listings l ON l.id = b.listing_id "
                " WHERE l.seller_id = ? AND b.bidder_id <> ? "
                " GROUP BY b.bidder_id, l.id",
                (uid, uid)).fetchall()
            for r in rows or ():
                add(r["other"], f'bid on your lot #{r["lot"]}', float(r["seq"] or 0))

            rows = conn.execute(
                "SELECT l.seller_id AS other, l.id AS lot, l.title AS title, "
                "       MAX(b.id) AS seq "
                "  FROM land_bids b JOIN land_listings l ON l.id = b.listing_id "
                " WHERE b.bidder_id = ? AND l.seller_id <> ? "
                " GROUP BY l.seller_id, l.id",
                (uid, uid)).fetchall()
            for r in rows or ():
                add(r["other"], f'listed lot #{r["lot"]} you bid on', float(r["seq"] or 0))

            rows = conn.execute(
                "SELECT user_lo, user_hi, last_message_at FROM vt_message_threads "
                " WHERE user_lo = ? OR user_hi = ?", (uid, uid)).fetchall()
            for r in rows or ():
                other = r["user_hi"] if str(r["user_lo"]) == uid else r["user_lo"]
                add(other, "you already have a thread", float(r["last_message_at"] or 0))
    except Exception as e:
        # A contact list we cannot build is an empty contact list, not an open one.
        log.exception("[messages] contact lookup failed for %s: %s", uid, e)
        return []

    for sid in shell.staff_ids():
        if str(sid) != uid:
            add(str(sid), "V Tech staff", 0)

    rows = sorted(out.values(), key=lambda c: -c["at"])
    for c in rows:
        c["name"] = _display_name(c["user_id"])
    return rows


def _has_wallet(uid: str) -> bool:
    """True when this id is a player in this economy at all."""
    try:
        with _core_db().db() as conn:
            row = conn.execute("SELECT 1 FROM balances WHERE user_id = ?",
                               (str(uid),)).fetchone()
        return row is not None
    except Exception:
        return False


def _may_message(sender: str, target: str) -> bool:
    """The authorisation for OPENING a thread. Reading one is a separate check
    (`_participant_thread`) and neither substitutes for the other."""
    sender, target = str(sender), str(target)
    if not target or sender == target:
        return False
    staff = {str(s) for s in shell.staff_ids()}
    if target in staff:
        return True
    if sender in staff:
        return _has_wallet(target)
    return any(c["user_id"] == target for c in _contacts(sender))


#: ONE refusal for every "no". Not-a-player, never-dealt-with and staff-only all
#: return this exact string, because three distinct refusals is a lookup service for
#: who holds an account here.
_CANNOT_MESSAGE = ("That player cannot be messaged from here. You can message people "
                   "you have dealt with — a lot you bid on, or a bidder on yours — "
                   "and V Tech staff.")


# ══════════════════════════════════════════════════════════════════════════
# Reads
# ══════════════════════════════════════════════════════════════════════════

def _thread_row(tid: int) -> Optional[dict]:
    _ensure_tables()
    with _core_db().db() as conn:
        row = conn.execute("SELECT * FROM vt_message_threads WHERE id = ?",
                           (int(tid),)).fetchone()
    return dict(row) if row else None


def _participant_thread(tid: Any, uid: str) -> Optional[dict]:
    """The thread, ONLY IF `uid` is one of its two participants — otherwise None.

    Authorisation is this participant check, on every read, and it is a property of the
    row rather than of the URL. Guessing an id gets you None and a 404, and a 404 is
    what a thread that does not exist returns too, so the id space tells a stranger
    nothing about which threads are real.
    """
    try:
        tid_i = int(str(tid).strip())
    except (TypeError, ValueError):
        return None
    row = _thread_row(tid_i)
    if not row:
        return None
    uid = str(uid)
    if uid != str(row["user_lo"]) and uid != str(row["user_hi"]):
        return None
    return row


def _other_side(thread: dict, uid: str) -> str:
    return str(thread["user_hi"]) if str(thread["user_lo"]) == str(uid) else str(thread["user_lo"])


def _threads_for(uid: str, limit: int = INBOX_LIMIT) -> list:
    """My threads, most recent activity first, each with MY unread count.

    A UNION of two indexed lookups rather than `WHERE user_lo=? OR user_hi=?`, so the
    plan is two index scans on `ix_vt_thread_lo`/`ix_vt_thread_hi` instead of a table
    scan the day this table is not tiny.

    The unread figure is a COUNT against the watermark, computed here, every time. It
    is never stored and never incremented.
    """
    _ensure_tables()
    uid = str(uid)
    sql = """
    WITH mine AS (
        SELECT * FROM vt_message_threads WHERE user_lo = :me
        UNION ALL
        SELECT * FROM vt_message_threads WHERE user_hi = :me
    )
    SELECT t.id, t.user_lo, t.user_hi, t.last_message_at, t.last_message_id,
           COALESCE(r.last_read_message_id, 0) AS watermark,
           (SELECT COUNT(*) FROM vt_messages m
             WHERE m.thread_id = t.id
               AND m.sender_id <> :me
               AND m.id > COALESCE(r.last_read_message_id, 0)) AS unread,
           (SELECT m2.body FROM vt_messages m2
             WHERE m2.thread_id = t.id ORDER BY m2.id DESC LIMIT 1) AS last_body,
           (SELECT m2.sender_id FROM vt_messages m2
             WHERE m2.thread_id = t.id ORDER BY m2.id DESC LIMIT 1) AS last_sender
      FROM mine t
      LEFT JOIN vt_message_reads r ON r.thread_id = t.id AND r.user_id = :me
     ORDER BY t.last_message_at DESC, t.id DESC
     LIMIT :lim
    """
    with _core_db().db() as conn:
        rows = conn.execute(sql, {"me": uid, "lim": int(limit)}).fetchall()
    out = []
    for r in rows or ():
        d = dict(r)
        d["other"] = _other_side(d, uid)
        d["other_name"] = _display_name(d["other"])
        out.append(d)
    return out


def _unread_total(uid: str) -> int:
    """Every unread message addressed to `uid`, across every thread. Derived."""
    _ensure_tables()
    uid = str(uid)
    sql = """
    WITH mine AS (
        SELECT id FROM vt_message_threads WHERE user_lo = :me
        UNION ALL
        SELECT id FROM vt_message_threads WHERE user_hi = :me
    )
    SELECT COUNT(*) AS c
      FROM vt_messages m
      JOIN mine t ON t.id = m.thread_id
      LEFT JOIN vt_message_reads r ON r.thread_id = m.thread_id AND r.user_id = :me
     WHERE m.sender_id <> :me
       AND m.id > COALESCE(r.last_read_message_id, 0)
    """
    with _core_db().db() as conn:
        row = conn.execute(sql, {"me": uid}).fetchone()
    return int(row["c"] if row else 0)


def _messages_page(tid: int, before: Optional[int] = None, limit: int = PAGE_SIZE) -> tuple:
    """`(rows_oldest_first, has_more)` for one page of a thread, newest page first.

    Reads `limit + 1` and drops the extra, so "is there an older page" is answered by
    the same index scan rather than by a second COUNT over the whole thread.
    """
    _ensure_tables()
    args: list = [int(tid)]
    sql = "SELECT * FROM vt_messages WHERE thread_id = ?"
    if before:
        sql += " AND id < ?"
        args.append(int(before))
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit) + 1)
    with _core_db().db() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    return rows, has_more


# ══════════════════════════════════════════════════════════════════════════
# Writes — all three of them, and there is no fourth
# ══════════════════════════════════════════════════════════════════════════

def _mark_read(tid: int, uid: str, up_to: int) -> int:
    """Advance `uid`'s watermark in thread `tid` to `up_to`. Returns the watermark.

    MONOTONIC IN SQL, not in Python. The upsert's conflict arm is
    `MAX(last_read_message_id, excluded…)`, so two tabs racing — one of them holding a
    stale page — cannot un-read a message, and neither can a client that posts a number
    it read a minute ago. A read-then-write here is precisely the stale-read counter bug
    this project has already paid for once.
    """
    _ensure_tables()
    up_to = max(0, int(up_to))
    now = _now()
    with _core_db().db() as conn:
        conn.execute(
            "INSERT INTO vt_message_reads (thread_id, user_id, last_read_message_id, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(thread_id, user_id) DO UPDATE SET "
            "  last_read_message_id = MAX(vt_message_reads.last_read_message_id, excluded.last_read_message_id), "
            "  updated_at = excluded.updated_at",
            (int(tid), str(uid), up_to, now))
        row = conn.execute("SELECT last_read_message_id FROM vt_message_reads "
                           " WHERE thread_id = ? AND user_id = ?",
                           (int(tid), str(uid))).fetchone()
    return int(row["last_read_message_id"]) if row else 0


def _get_or_create_thread(a: str, b: str, creator: str) -> tuple:
    """`(thread_id, created)` for the pair. CLAIM FIRST, THEN READ THE ROWCOUNT.

    `INSERT OR IGNORE` against the UNIQUE pair index is the claim: of two concurrent
    first-messages exactly one gets `rowcount == 1`, and the loser reads back the row
    the winner wrote. A `SELECT` followed by an `INSERT` would let both through under
    exactly the conditions that produce the duplicate — a double-clicked button.
    """
    _ensure_tables()
    lo, hi = _pair(a, b)
    now = _now()
    with _core_db().db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO vt_message_threads "
            "  (user_lo, user_hi, created_at, created_by, last_message_at, last_message_id) "
            "VALUES (?,?,?,?,0,0)", (lo, hi, now, str(creator)))
        created = (cur.rowcount == 1)
        row = conn.execute("SELECT id FROM vt_message_threads WHERE user_lo=? AND user_hi=?",
                           (lo, hi)).fetchone()
    if row is None:  # pragma: no cover - only if the row vanished mid-transaction
        raise RuntimeError("thread row disappeared immediately after insert")
    return int(row["id"]), created


class _RateLimited(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _check_rate(uid: str, opening_thread: bool) -> None:
    """Per-sender limits, counted off `vt_messages` itself. No counter to go stale.

    Raises `_RateLimited`, which the caller turns into a `NoEffect` — nothing has been
    written at this point, so the form key is released and the player's next attempt
    uses a fresh, working form rather than being told their key is spent.
    """
    now = _now()
    with _core_db().db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM vt_messages "
                           " WHERE sender_id = ? AND created_at > ?",
                           (str(uid), now - 60)).fetchone()
        recent = int(row["c"] if row else 0)
        if recent >= SEND_PER_MINUTE:
            raise _RateLimited(
                f"You have sent {recent} messages in the last minute, which is the "
                f"limit ({SEND_PER_MINUTE}). Nothing was sent. Wait a moment and "
                f"send it again.")
        if opening_thread:
            row = conn.execute(
                "SELECT MAX(created_at) AS t FROM vt_message_threads "
                " WHERE created_by = ?", (str(uid),)).fetchone()
            last = float((row["t"] if row else 0) or 0)
            if last and now - last < NEW_THREAD_COOLDOWN:
                raise _RateLimited(
                    f"You started a conversation {int(now - last)} seconds ago. New "
                    f"conversations are limited to one every {NEW_THREAD_COOLDOWN} "
                    f"seconds. Nothing was sent.")


def _append_message(tid: int, sender: str, body: str, key: str) -> tuple:
    """`(message_id, written)` — append one row. THE ONLY INSERT INTO `vt_messages`.

    `INSERT OR IGNORE` against the UNIQUE `idem_key` index, rowcount read. This is the
    floor under the form key, not a replacement for it: `shell.money_post` has already
    claimed the key, so a second request normally never reaches here. If one does —
    a claim table restored from a backup, a key replayed after a sweep — it writes
    NOTHING and the caller reports the message that already exists. Three outcomes,
    never two: written / already-written / (on any exception) the key stays claimed and
    the outcome is UNKNOWN.
    """
    _ensure_tables()
    now = _now()
    with _core_db().db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO vt_messages (thread_id, sender_id, body, created_at, idem_key) "
            "VALUES (?,?,?,?,?)", (int(tid), str(sender), str(body), now, str(key)))
        written = (cur.rowcount == 1)
        if written:
            mid = int(cur.lastrowid)
        else:
            row = conn.execute("SELECT id, thread_id FROM vt_messages WHERE idem_key = ?",
                               (str(key),)).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("message insert ignored but no row carries the key")
            mid = int(row["id"])
        # Ordering key moves FORWARD only. rowcount 0 here is not an error: it means a
        # later message already advanced it, and the inbox is already correct.
        cur2 = conn.execute(
            "UPDATE vt_message_threads SET last_message_at = ?, last_message_id = ? "
            " WHERE id = ? AND last_message_id < ?", (now, mid, int(tid), mid))
        if cur2.rowcount == 0 and written:
            log.debug("[messages] thread %s ordering key already ahead of message %s",
                      tid, mid)
    return mid, written


# ══════════════════════════════════════════════════════════════════════════
# Rendering — server-side, escaped here, in the house theme
# ══════════════════════════════════════════════════════════════════════════

#: Section-local CSS. Zero border-radius, no emoji, mono figures with tabular-nums,
#: two type colours. Nothing here overrides a shell token; it only adds the shapes a
#: conversation needs and the shell has no opinion about.
_CSS = """
<style>
.msg-wrap{display:grid;grid-template-columns:1fr;gap:14px}
.msg-empty{padding:16px 0;color:var(--muted);font-size:12px}
.msg-unread{display:inline-flex;align-items:center;justify-content:center;min-width:20px;
  height:18px;padding:0 5px;background:var(--accent);color:#000;font-family:var(--font-data);
  font-size:10.5px;font-weight:600;font-variant-numeric:tabular-nums}
.msg-zero{color:var(--faint)}
.msg-snip{color:var(--text-body);font-family:var(--font-ui);font-size:12px}
.msg-row-new td{background:rgba(34,255,122,.04)}
.msg-thread{border:1px solid var(--border);background:var(--surface);padding:14px 16px;
  max-height:none}
.msg{padding:10px 0;border-bottom:1px solid var(--border)}
.msg:last-child{border-bottom:none}
.msg-h{display:flex;align-items:baseline;gap:10px;margin-bottom:3px}
.msg-who{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted)}
.msg-mine .msg-who{color:var(--accent)}
.msg-when{font-family:var(--font-data);font-size:10.5px;color:var(--faint)}
.msg-b{font-size:13px;line-height:1.55;color:var(--text);white-space:pre-wrap;
  overflow-wrap:anywhere}
.msg-form textarea{width:100%;background:var(--panel2);border:1px solid var(--border);
  color:var(--text);font-family:var(--font-ui);font-size:13px;line-height:1.5;padding:10px 12px;
  resize:vertical;min-height:74px}
.msg-form textarea:focus{outline:none;border-color:var(--border-strong)}
.msg-form select{background:var(--panel2);border:1px solid var(--border);color:var(--text);
  font-family:var(--font-ui);font-size:12px;padding:7px 10px}
.msg-foot{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:9px}
.msg-count{font-family:var(--font-data);font-size:10.5px;color:var(--faint)}
.msg-err{margin-top:9px;font-size:12px;color:var(--red)}
</style>
"""


def _snippet(body: Any, limit: int = 78) -> str:
    """One line of a message for the inbox table. Escaped like every other render."""
    s = " ".join(str(body or "").split())
    if len(s) > limit:
        s = s[:limit - 1].rstrip() + "…"
    return esc(s)


def _inbox_body(uid: str, contacts: list, keys: dict, error: str = "") -> str:
    """The inbox. A TABLE, because it is a repeating relational list — one row per
    thread, the number right-aligned in mono, the name left. Not cards."""
    threads = _threads_for(uid)
    total = sum(int(t["unread"] or 0) for t in threads)

    rows = []
    for t in threads:
        unread = int(t["unread"] or 0)
        badge = (f'<span class="msg-unread">{unread}</span>' if unread
                 else '<span class="msg-zero">0</span>')
        who = "You" if str(t["last_sender"] or "") == str(uid) else esc(t["other_name"])
        snip = _snippet(t["last_body"]) if t["last_body"] else '<span class="muted">no messages</span>'
        rows.append(
            f'<tr class="{"msg-row-new" if unread else ""}" '
            f'onclick="location.href=\'/messages/t/{int(t["id"])}\'" style="cursor:pointer">'
            f'<td><a href="/messages/t/{int(t["id"])}">{esc(t["other_name"])}</a></td>'
            f'<td class="msg-snip" style="text-align:left"><span class="muted">{who}:</span> {snip}</td>'
            f'<td>{badge}</td>'
            f'<td title="{esc(_stamp(t["last_message_at"]))}">{esc(_ago(t["last_message_at"]))}</td>'
            f'</tr>')

    if rows:
        table = ('<table><thead><tr><th>Player</th><th style="text-align:left">Last message</th>'
                 '<th>Unread</th><th>Activity</th></tr></thead><tbody>'
                 + "".join(rows) + '</tbody></table>')
    else:
        # THE EMPTY STATE. One muted line, no illustration, no suggestion, no fake row.
        # This ships empty and will be empty until two players use it, and saying so
        # plainly is the only honest thing to put here.
        table = ('<div class="msg-empty">No conversations yet. Nothing has been sent on '
                 'this site.</div>')

    opts = "".join(
        f'<option value="{esc(c["user_id"])}" data-key="{esc(keys.get(c["user_id"], ""))}">'
        f'{esc(c["name"])} — {esc(c["via"])}</option>'
        for c in contacts)
    if contacts:
        form = f"""
<div class="tile s12 msg-form">
  <div class="tile-h">Start a conversation</div>
  <div class="row" style="margin-bottom:9px">
    <select id="mTo">{opts}</select>
  </div>
  <textarea id="mBodyNew" maxlength="{BODY_MAX}"
    placeholder="Write a message. It cannot be edited or deleted once sent."></textarea>
  <div class="msg-foot">
    <span class="msg-count" id="mCountNew">0 / {BODY_MAX}</span>
    <button class="btn" id="mSendNew" onclick="sendNew()">Send</button>
  </div>
  <div class="msg-err" id="mErrNew">{esc(error)}</div>
</div>"""
    else:
        form = ('<div class="tile s12"><div class="tile-h">Start a conversation</div>'
                '<div class="msg-empty">You have not dealt with anyone here yet, so there '
                'is nobody to write to. Bid on a lot, or list one, and the other side '
                'becomes reachable.</div></div>')

    return f"""{_CSS}
<div class="page-head">
  <div>
    <h1>Messages</h1>
    <div class="page-sub">One-to-one, between two players on this site. Messages are
    permanent: nothing here can be edited or deleted, by anyone, so what you read is
    what was sent.</div>
  </div>
</div>
<div class="section-h">Inbox<span class="msg-count" style="margin-left:10px">{total} unread</span></div>
{table}
{form}
"""


def _message_html(m: dict, uid: str) -> str:
    """One message. `esc(m["body"])` is the ONLY place a body reaches a page, and it is
    escaped here rather than at write, so the stored row stays exactly what was typed."""
    mine = str(m["sender_id"]) == str(uid)
    who = "You" if mine else esc(_display_name(m["sender_id"]))
    return (f'<div class="msg{" msg-mine" if mine else ""}">'
            f'<div class="msg-h"><span class="msg-who">{who}</span>'
            f'<span class="msg-when" title="{esc(_stamp(m["created_at"]))}">'
            f'{esc(_ago(m["created_at"]))}</span></div>'
            f'<div class="msg-b">{esc(m["body"])}</div></div>')


def _thread_body(uid: str, thread: dict, key: str, before: Optional[int],
                 key_note: str = "") -> str:
    tid = int(thread["id"])
    other = _other_side(thread, uid)
    rows, has_more = _messages_page(tid, before)
    newest = max((int(m["id"]) for m in rows), default=0)

    if rows:
        convo = "".join(_message_html(m, uid) for m in rows)
    else:
        convo = ('<div class="msg-empty">Nothing has been sent in this conversation '
                 'yet.</div>')

    older = ""
    if has_more and rows:
        older = (f'<div style="margin-bottom:10px"><a class="btn ghost sm" '
                 f'href="/messages/t/{tid}?before={int(rows[0]["id"])}">Older messages</a></div>')

    # Decision 2, said out loud to the one person entitled to know it, and only inside
    # a thread this viewer is a participant of. It reports the absence of a WALLET —
    # never a login state, which this module does not read.
    never = ""
    if not _has_wallet(other):
        never = ('<div class="notebox">This player has no wallet on V Tech yet. The '
                 'message is stored and waits for them.</div>')

    note = f'<div class="notebox">{esc(key_note)}</div>' if key_note else ""

    return f"""{_CSS}
<div class="page-head">
  <div>
    <h1>{esc(_display_name(other))}</h1>
    <div class="page-sub">Every message below is permanent — neither side can edit or
    delete one. Nobody is told when you read a message.</div>
  </div>
  <a class="btn ghost sm" href="/messages">All conversations</a>
</div>
{older}
<div class="msg-thread" id="mConvo">{convo}</div>
{never}{note}
<div class="tile s12 msg-form" style="margin-top:14px">
  <div class="tile-h">Reply</div>
  <textarea id="mBody" maxlength="{BODY_MAX}"
    placeholder="Write a message. It cannot be edited or deleted once sent."></textarea>
  <div class="msg-foot">
    <span class="msg-count" id="mCount">0 / {BODY_MAX}</span>
    <button class="btn" id="mSend" onclick="sendReply()">Send</button>
  </div>
  <div class="msg-err" id="mErr"></div>
</div>
<script>window.MSG = {{tid:{tid}, key:{json.dumps(key)}, newest:{newest}}};</script>
"""


_INBOX_JS = r"""
function bindCount(taId, outId){
  const ta = document.getElementById(taId), out = document.getElementById(outId);
  if(!ta || !out) return;
  const upd = () => { out.textContent = ta.value.length + ' / __MAX__'; };
  ta.addEventListener('input', upd); upd();
}
bindCount('mBodyNew', 'mCountNew');

async function sendNew(){
  const btn = document.getElementById('mSendNew');
  const sel = document.getElementById('mTo');
  const ta  = document.getElementById('mBodyNew');
  const err = document.getElementById('mErrNew');
  if(!btn || btn.disabled) return;          /* the UI half of claim-first */
  const opt = sel.options[sel.selectedIndex];
  err.textContent = '';
  if(!ta.value.trim()){ err.textContent = 'Write something first.'; return; }
  btn.disabled = true;
  const r = await post('/api/messages/send',
    {to: sel.value, body: ta.value, idempotency_key: opt ? opt.dataset.key : ''});
  if(r.ok){ location.href = '/messages/t/' + r.thread_id; return; }
  err.textContent = r.error || 'The message was not sent.';
  btn.disabled = false;
}
loadMe().then(() => { renderStrip(); });
""".replace("__MAX__", str(BODY_MAX))


_THREAD_JS = r"""
function bindCount(taId, outId){
  const ta = document.getElementById(taId), out = document.getElementById(outId);
  if(!ta || !out) return;
  const upd = () => { out.textContent = ta.value.length + ' / __MAX__'; };
  ta.addEventListener('input', upd); upd();
}
bindCount('mBody', 'mCount');

async function sendReply(){
  const btn = document.getElementById('mSend');
  const ta  = document.getElementById('mBody');
  const err = document.getElementById('mErr');
  if(!btn || btn.disabled) return;          /* the UI half of claim-first */
  err.textContent = '';
  if(!ta.value.trim()){ err.textContent = 'Write something first.'; return; }
  btn.disabled = true;
  const r = await post('/api/messages/send',
    {thread_id: MSG.tid, body: ta.value, idempotency_key: MSG.key});
  if(r.ok){ location.reload(); return; }
  err.textContent = r.error || 'The message was not sent.';
  btn.disabled = false;
}

/* Opening a thread marks what is ON THIS PAGE as read — never more. The watermark
   only ever moves forward, server-side, so this cannot bury a message that arrived
   after the page was rendered. */
async function markRead(){
  if(!MSG.newest) return;
  const r = await post('/api/messages/read', {thread_id: MSG.tid, up_to: MSG.newest});
  if(r && r.ok) refreshUnread();
}
loadMe().then(() => { renderStrip(); markRead(); });
""".replace("__MAX__", str(BODY_MAX))


# ══════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════

def _resume_key(uid: str, purpose: str) -> tuple:
    """`(key, note)` — reuse an in-flight key for this subject rather than minting.

    A send whose outcome is unknown leaves its key claimed (that is `money_post`'s
    contract and it is the right one). If the re-render minted a FRESH key, the retry
    would be a key the claim table has never seen — which is the double, one level up,
    and it is the bug `shell.in_flight_keys` was written for. So the render path asks
    for its own subject's stuck key and hands the SAME one back, with the reason in
    words instead of a silent 409 on submit.
    """
    subject = purpose.split(":", 1)[1] if ":" in purpose else ""
    try:
        for row in shell.in_flight_keys(uid, SEND_ENDPOINT):
            if row.get("subject") == subject:
                return row["key"], (
                    "A message you sent has not been confirmed yet. This form carries "
                    "the same confirmation as that one, so sending again cannot post it "
                    "twice — it will be refused instead."
                    + (f" ({row['note']})" if row.get("note") else ""))
    except Exception:  # pragma: no cover
        log.warning("[messages] in-flight key lookup failed", exc_info=True)
    return shell.mint_form_key(uid, purpose), ""


async def h_inbox(request):
    """`GET /messages` — the inbox. Logged out gets 401 and the sign-in card."""
    sess, refusal = shell.require_page_session(request)
    if refusal is not None:
        return refusal
    uid = str(sess["user_id"])
    contacts = _contacts(uid)
    keys = {}
    for c in contacts:
        k, _note = _resume_key(uid, f"message:new:{c['user_id']}")
        keys[c["user_id"]] = k
    return shell.page("Messages", "messages", _inbox_body(uid, contacts, keys), _INBOX_JS)


async def h_thread(request):
    """`GET /messages/t/{tid}` — one conversation.

    A stranger and a non-existent thread get the SAME 404 page: the participant check
    is `_participant_thread`, and it refuses before a single body is read out of the
    database, so no branch of this handler can render a message to somebody who is not
    in the thread.
    """
    sess, refusal = shell.require_page_session(request)
    if refusal is not None:
        return refusal
    uid = str(sess["user_id"])
    thread = _participant_thread(request.match_info.get("tid"), uid)
    if thread is None:
        body = (_CSS + '<div class="page-head"><div><h1>Conversation</h1>'
                '<div class="page-sub">No such conversation.</div></div></div>'
                '<div class="msg-empty">This conversation does not exist, or it is not '
                'yours.</div>')
        resp = shell.page("Messages", "messages", body, "loadMe().then(() => renderStrip());")
        resp.set_status(404)
        return resp
    try:
        before = int(request.query.get("before") or 0) or None
    except (TypeError, ValueError):
        before = None
    key, note = _resume_key(uid, f"message:t:{int(thread['id'])}")
    return shell.page(f"{_display_name(_other_side(thread, uid))} · Messages", "messages",
                      _thread_body(uid, thread, key, before, note), _THREAD_JS)


def _send_purpose(body: dict) -> str:
    """The form key's subject: the thread being replied to, or the player being written
    to for the first time. A bare `"message"` purpose would let a key minted on one
    conversation post into another — WEB_ATTACK finding 7, in a different section."""
    tid = str((body or {}).get("thread_id") or "").strip()
    if tid:
        return f"message:t:{int(tid)}"
    to = str((body or {}).get("to") or "").strip()
    if to:
        return f"message:new:{to}"
    raise ValueError("neither thread_id nor to")


def _clean_body(raw: Any) -> str:
    """The body, validated. Raises `shell.NoEffect` — nothing has been written."""
    s = str(raw if raw is not None else "").replace("\r\n", "\n").strip()
    if not s:
        raise shell.NoEffect("empty_message", "A message needs some text in it.", 400)
    if len(s) > BODY_MAX:
        raise shell.NoEffect(
            "message_too_long",
            f"That message is {len(s):,} characters and the limit is {BODY_MAX:,}. "
            f"Nothing was sent.", 400)
    return s


async def _do_send(sess, body, key):
    """The write. Runs at most once per form key — `shell.money_post` has already
    proved session, CSRF, key ownership, key subject and the claim before this is
    called, and records whatever it returns as the answer to a replay."""
    uid = str(sess["user_id"])
    text = _clean_body((body or {}).get("body"))

    tid_raw = str((body or {}).get("thread_id") or "").strip()
    if tid_raw:
        thread = _participant_thread(tid_raw, uid)
        if thread is None:
            # Same refusal for "no such thread" and "not yours" — see `_participant_thread`.
            raise shell.NoEffect("no_such_thread",
                                 "That conversation does not exist, or it is not yours.", 404)
        tid = int(thread["id"])
        other = _other_side(thread, uid)
        opening = False
    else:
        other = str((body or {}).get("to") or "").strip()
        if other == uid:
            raise shell.NoEffect("self_message", "You cannot message yourself.", 400)
        if not _may_message(uid, other):
            raise shell.NoEffect("cannot_message", _CANNOT_MESSAGE, 403)
        opening = True
        tid = 0

    try:
        _check_rate(uid, opening)
    except _RateLimited as e:
        raise shell.NoEffect("rate_limited", e.message, 429)

    if opening:
        tid, _created = _get_or_create_thread(uid, other, uid)

    mid, written = _append_message(tid, uid, text, key)
    payload = {"ok": True, "thread_id": tid, "message_id": mid,
               "sent": _ago(_now()), "duplicate": not written}
    if not written:
        # Definitely-refused-as-a-second-write, not silently swallowed.
        payload["note"] = ("This confirmation had already posted a message. One message "
                           "exists, not two.")
    return 200, payload


async def h_send(request):
    """`POST /api/messages/send`.

    Goes through `shell.money_post` even though it moves no coins. The wrapper is not
    about money: it is session-then-CSRF-then-ignore-body-identity-then-verify-the-
    subject-bound-key-then-CLAIM-BEFORE-ACTING, in that order, with an unknown outcome
    leaving the key claimed. A message that must be written exactly once needs every
    one of those, and writing a second wrapper for this section would be a fourth
    pattern on a site that already agreed on one.

    The body is read once here, ahead of the wrapper, ONLY to raise the durable alarm:
    `money_post` logs a body-supplied identity but the queryable row lives in
    `hub_attack_log`, and a log line nobody greps is not a record. `request.json()`
    caches the payload, so the wrapper's own read sees the same bytes.
    """
    sess = shell.session_user(request)
    if sess:
        _note_identity(request, await shell.read_json(request), sess, SEND_ENDPOINT,
                       shell_log=False)
    return await shell.money_post(request, SEND_ENDPOINT, _send_purpose, _do_send)


async def h_mark_read(request):
    """`POST /api/messages/read` — advance my own watermark. No form key on purpose.

    This is idempotent by construction rather than by claim: the write is
    `MAX(old, new)`, so submitting it twice, out of order, or from a stale tab all
    converge on the same watermark. A single-use key here would be ceremony that can
    only fail, and a key that must be minted per page view for an action with no effect
    is a row written for every render.
    """
    sess, refusal = shell.require_post_session(request)
    if refusal is not None:
        return refusal
    body = await shell.read_json(request)
    _note_identity(request, body, sess, "messages/read")
    uid = str(sess["user_id"])

    thread = _participant_thread(body.get("thread_id"), uid)
    if thread is None:
        return shell.json_err("no_such_thread",
                              "That conversation does not exist, or it is not yours.", 404)
    try:
        up_to = int(str(body.get("up_to") or 0).strip() or 0)
    except (TypeError, ValueError):
        return shell.json_err("bad_up_to", "up_to must be a message id.", 400)

    mark = _mark_read(int(thread["id"]), uid, up_to)
    return shell.json_ok(thread_id=int(thread["id"]), read_up_to=mark,
                         unread=_unread_total(uid))


async def h_unread(request):
    """`GET /api/messages/unread` — the figure the nav badge shows, on every page.

    FOLDED INTO THE NAV RATHER THAN THE MONEY STRIP, deliberately. The strip is
    `available / held / savings / net` and every segment of it is coins; an unread
    count is not coins, and a non-money figure standing in that row is the kind of
    "figure with no unit" this project has a rule against. The nav tab is where a
    person looks for "is there something for me", and it is on every page of the site.

    Polled by `vt_web_shell`'s base script every 60s. It is a read, it is cheap
    (two index scans), and it is NOT exempt from `Restocker_web`'s per-IP throttle.
    """
    sess = shell.session_user(request)
    if not sess:
        # Not an error page — the badge simply does not exist for a logged-out visitor.
        return shell.json_err("not_logged_in", "Log in first.", 401)
    uid = str(sess["user_id"])
    threads = {int(t["id"]): int(t["unread"] or 0)
               for t in _threads_for(uid) if int(t["unread"] or 0)}
    return shell.json_ok(unread=_unread_total(uid), threads=threads)


# ══════════════════════════════════════════════════════════════════════════
# Mount
# ══════════════════════════════════════════════════════════════════════════

def _register_with_hub(key: str, label: str, path: str, order: int) -> None:
    """Tell `hub_web` this section exists, so the hub nav lists it. Same guarded shape
    as `banking_web` and `estates_web` — a section nothing links to is not shipped."""
    try:
        import hub_web
    except Exception:
        return
    try:
        hub_web.register_section(key, label, path, order=order)
    except Exception as e:  # pragma: no cover
        log.warning("[%s] could not register with the hub nav: %s", key, e)


def register_messages_routes(app) -> None:
    """Attach the messages section. Mirrors `estates_web.register_estates_routes`."""
    if web is None:  # pragma: no cover
        log.warning("[messages] aiohttp unavailable — messages not registered.")
        return
    shell.register_shell_routes(app)
    _register_with_hub("messages", "Messages", "/messages", order=50)
    app.router.add_get("/messages", h_inbox)
    app.router.add_get("/messages/t/{tid}", h_thread)
    app.router.add_post("/api/messages/send", h_send)
    app.router.add_post("/api/messages/read", h_mark_read)
    app.router.add_get("/api/messages/unread", h_unread)
    log.info("[messages] v%s registered (inbox · thread · send · read · unread)",
             MESSAGES_VERSION)
