"""panel_skus.py — pasteable addresses for panels.

WHY THIS EXISTS
---------------
Every support conversation in V Tech used to contain one of these:

    "which hive?"            -> "the one near spawn"      (there are four)
    "which lot?"             -> "the diamond one"          (there are nine)
    "paste me the market id" -> "greyhames"/"Greyhames"/"grey hames"

The house rule is *never make a user type an ID or an exact name*. A snowflake is
19 digits, a market id is an internal slug, an auction lot is an autoincrement.
None of those can be read off a phone screenshot or said out loud over voice.

A panel SKU is not an id. It is an **address**: four characters from an alphabet
with no `l`, `o`, `0` or `1` in it, printed in the footer of the panel it names.
A user reads it out, a staffer types `/go`, and the *same panel* opens on the
*same entity*. That is the whole feature.

SHAPE
-----
    0010.1.k7rq
    ^^^^ ^ ^^^^
    |    | +---- per-entity token: minted once, stored, never regenerated
    |    +------ panel sub-page (static)
    +----------- panel family (static)

Panels that address nothing (ManagerPanel, InvestorHub) print just `0060` — a
static code is still an address, it just resolves to "open this panel for me".

INVARIANTS
----------
1. **The token is minted once and stored.** Never `uuid4()` at render time: the
   code a user screenshotted at 14:02 must still open the same lot at 02:00.
2. **Mint is claim-first.** `INSERT` first and catch the constraint violation;
   the unique index `(kind, entity_id)` *is* the concurrency control. Two panels
   rendering the same hive in the same millisecond converge on one token —
   whoever lost the race re-reads the winner's row and uses it.
3. **Integer coins / no money here.** Minting is id assignment, not a user
   action. It is deliberately not logged and not rollbackable.
4. **Empty states are empty.** `resolve()` returns [] rather than a guess, and
   the caller says so in words.

This module owns its own DDL and bootstraps itself (`ensure_schema()` is
idempotent and cheap after the first call), so an existing `restocker.db` picks
the feature up with no migration step.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from typing import Callable, Optional

# ── The alphabet ────────────────────────────────────────────────────────────
# 32 characters. `l`, `o`, `0` and `1` are absent *on purpose*: they are the four
# glyphs that get misread when a human copies a code off a screenshot or hears it
# over voice chat. 32^4 = 1,048,576 four-character addresses.
ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
TOKEN_LEN = 4
MAX_TOKEN_LEN = 8          # grow rather than spin if the space ever gets crowded
_TRIES_PER_LEN = 8

# Characters a user might TYPE that can never appear in a token. We do not guess
# what they meant — we turn the position into a single-character SQL wildcard and
# let the database answer. One hit -> jump. Several -> picker. None -> say so.
_AMBIGUOUS_INPUT = set("lo01|!")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS panel_skus (
    token       TEXT PRIMARY KEY,          -- the address tail, lowercase
    kind        TEXT NOT NULL,             -- entity family: market / hive / lot / ...
    entity_id   TEXT NOT NULL,             -- str() of the entity's natural key
    panel_key   TEXT NOT NULL,             -- which panel this address opens
    panel_code  TEXT NOT NULL,             -- static base.sub at mint time
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
-- THE concurrency control for minting. Not an optimisation: the constraint is
-- what makes "insert and catch" a claim.
CREATE UNIQUE INDEX IF NOT EXISTS idx_panel_skus_entity
    ON panel_skus(kind, entity_id);
CREATE INDEX IF NOT EXISTS idx_panel_skus_panel ON panel_skus(panel_key);
"""

_schema_ready = False


def _db():
    import Restocker_db as _d
    return _d


def ensure_schema() -> None:
    """Idempotent. Safe to call from anywhere, including a render path."""
    global _schema_ready
    if _schema_ready:
        return
    with _db().db() as conn:
        conn.executescript(_SCHEMA)
    _schema_ready = True


# ── Panel registry ──────────────────────────────────────────────────────────
class Panel:
    """One panel family.

    `kind` is None for panels that address nothing (they print a static code).
    `opener` is filled in by whichever cog owns the panel via register_opener();
    this module never imports views.* itself, so a build missing a view module
    degrades to "that panel is not available in this build" instead of failing
    to import.
    """

    __slots__ = ("key", "code", "sub", "kind", "title", "opener")

    def __init__(self, key: str, code: str, sub: str, kind: Optional[str], title: str):
        self.key = key
        self.code = code
        self.sub = sub
        self.kind = kind
        self.title = title
        self.opener: Optional[Callable] = None

    @property
    def base(self) -> str:
        return f"{self.code}.{self.sub}" if self.sub else self.code


# Codes are STABLE. Adding a panel takes the next free block; never renumber,
# because someone has last month's screenshot.
PANELS: dict[str, Panel] = {
    p.key: p for p in (
        Panel("market",    "0010", "1", "market", "Market settings"),
        Panel("hive",      "0020", "1", "hive",   "Hive settings"),
        Panel("team",      "0030", "1", "team",   "Team settings"),
        Panel("me",        "0040", "1", "user",   "Loyalty hub"),
        Panel("item",      "0050", "1", "item",   "Item panel"),
        Panel("manager",   "0060", "",  None,     "Manager panel"),
        Panel("investor",  "0070", "",  None,     "Investor hub"),
        Panel("lot",       "0080", "1", "lot",    "Auction lot"),
        Panel("stock",     "0090", "1", "market", "Stock panel"),
    )
}

# kind -> the panel that owns it, for reverse lookup from a bare token.
_KIND_PANEL = {p.kind: p for p in PANELS.values() if p.kind}


def register_opener(panel_key: str, coro: Callable) -> None:
    """Bind `panel_key` to `async def opener(interaction, entity_id) -> bool`.

    Returning False (or raising) means "I could not open it" — `/go` then shows
    the honest empty state rather than a blank ephemeral.
    """
    p = PANELS.get(panel_key)
    if p is None:
        raise KeyError(f"unknown panel key {panel_key!r}")
    p.opener = coro


# ── Minting ─────────────────────────────────────────────────────────────────
def _gen(n: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def peek(kind: str, entity_id) -> Optional[str]:
    """The stored token for this entity, or None. Never mints."""
    ensure_schema()
    with _db().db() as conn:
        row = conn.execute(
            "SELECT token FROM panel_skus WHERE kind=? AND entity_id=?",
            (str(kind), str(entity_id))).fetchone()
    return row["token"] if row else None


def mint(kind: str, entity_id, panel_key: str) -> str:
    """Return this entity's address token, minting one on first call.

    CLAIM-FIRST. The read below is a fast path only; the *authority* is the
    INSERT. On IntegrityError we cannot tell from the exception whether we hit
    the token PK or the (kind, entity_id) unique index, so we ask: if a row for
    this entity now exists, a concurrent mint won and we adopt its token; if not,
    it was a token collision and we roll a new one.
    """
    ensure_schema()
    kind = str(kind)
    eid = str(entity_id)
    panel = PANELS.get(panel_key)
    code = panel.base if panel else str(panel_key)

    existing = peek(kind, eid)
    if existing:
        return existing

    length = TOKEN_LEN
    while length <= MAX_TOKEN_LEN:
        for _ in range(_TRIES_PER_LEN):
            token = _gen(length)
            try:
                with _db().db() as conn:
                    conn.execute(
                        "INSERT INTO panel_skus (token, kind, entity_id, panel_key, panel_code) "
                        "VALUES (?,?,?,?,?)",
                        (token, kind, eid, str(panel_key), code))
                return token
            except sqlite3.IntegrityError:
                won_by_someone_else = peek(kind, eid)
                if won_by_someone_else:
                    return won_by_someone_else
                continue          # token collision — roll again
        length += 1               # the 4-char space is crowded; widen, don't spin
    raise RuntimeError("panel_skus: exhausted the token space up to "
                       f"{MAX_TOKEN_LEN} characters")


def address(panel_key: str, entity_id=None) -> str:
    """The full pasteable address for a panel, e.g. `0010.1.k7rq` or `0060`."""
    panel = PANELS.get(panel_key)
    if panel is None:
        return str(panel_key)
    if entity_id is None or panel.kind is None:
        return panel.base or panel.code
    return f"{panel.base}.{mint(panel.kind, entity_id, panel_key)}"


def forget(kind: str, entity_id) -> bool:
    """Release an address when its entity is deleted for good.

    Deliberately NOT called on soft state changes (a sold lot keeps its address
    so "what happened to k7rq" is still answerable).
    """
    ensure_schema()
    with _db().db() as conn:
        cur = conn.execute("DELETE FROM panel_skus WHERE kind=? AND entity_id=?",
                           (str(kind), str(entity_id)))
        return cur.rowcount > 0


# ── Resolving ───────────────────────────────────────────────────────────────
def normalise(raw: str) -> str:
    """Strip an address down to its token.

    Accepts everything a human plausibly pastes: `0010.1.k7rq`, `#k7rq`,
    `K7RQ`, `k 7 r q`, and a bare `k7rq`.
    """
    s = (raw or "").strip().lower()
    s = s.split("#")[-1]
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    return re.sub(r"[^a-z0-9|!]", "", s)


def _pattern(token: str) -> Optional[str]:
    """SQL LIKE pattern for a typed token, `_` wherever the user typed a glyph
    that can never occur in a real token. We refuse to guess which character
    they misread — the database is a better guesser than we are."""
    if not token:
        return None
    out = []
    for ch in token:
        if ch in _AMBIGUOUS_INPUT:
            out.append("_")
        elif ch in ALPHABET:
            out.append(ch)
        else:
            return None            # a character that is neither valid nor a known misread
    return "".join(out)


def resolve(raw: str) -> list[dict]:
    """Candidate rows for a typed address. 0 = say so, 1 = jump, >1 = picker."""
    ensure_schema()
    token = normalise(raw)
    pat = _pattern(token)
    if not pat:
        return []
    sql = ("SELECT token, kind, entity_id, panel_key, panel_code FROM panel_skus "
           "WHERE token LIKE ? ORDER BY token LIMIT 25")
    with _db().db() as conn:
        rows = conn.execute(sql, (pat,)).fetchall()
        if not rows and len(pat) >= TOKEN_LEN:
            # A token that had to grow past four characters still answers to the
            # four a user read off a screenshot, as long as it stays unambiguous.
            rows = conn.execute(sql, (pat + "%",)).fetchall()
    return [dict(r) for r in rows]


# ── Real names, everywhere a user looks ─────────────────────────────────────
def describe(kind: str, entity_id, *, guild=None) -> Optional[str]:
    """The human name of an entity, or None if it no longer exists.

    None is meaningful: `/go` uses it to tell the user the address pointed at a
    lot that has since been removed, instead of opening an empty panel.
    """
    d = _db()
    eid = str(entity_id)
    try:
        with d.db() as conn:
            if kind == "market":
                r = conn.execute("SELECT name FROM markets WHERE market_id=?", (eid,)).fetchone()
                return r["name"] if r else None
            if kind == "hive":
                r = conn.execute("SELECT location, user_tag FROM hive_claims WHERE location=?",
                                 (eid,)).fetchone()
                if not r:
                    return None
                return f"{r['location']} (kept by {r['user_tag']})" if r["user_tag"] else r["location"]
            if kind == "lot":
                r = conn.execute(
                    "SELECT title, kind, status FROM land_listings WHERE id=?", (eid,)).fetchone()
                if not r:
                    return None
                title = r["title"] or f"{r['kind']} lot"
                if r["status"] == "active":
                    return title
                # `sold` / `expired` / `cancelled` are already English; `rolled_back`
                # is not, and this string is read by a person picking a lot.
                label = {"rolled_back": "sale rolled back"}.get(r["status"], r["status"])
                return f"{title} · {label}"
            if kind == "item":
                r = conn.execute("SELECT name FROM items WHERE name=?", (eid,)).fetchone()
                return r["name"] if r else None
            if kind == "team":
                r = conn.execute("SELECT manager_id FROM team_settings WHERE manager_id=?",
                                 (eid,)).fetchone()
                if not r:
                    r = conn.execute("SELECT manager_id FROM team_members WHERE manager_id=? LIMIT 1",
                                     (eid,)).fetchone()
                if not r:
                    return None
                return f"{_member_name(guild, eid)}'s team"
            if kind == "user":
                return _member_name(guild, eid)
    except Exception:
        return None
    return None


def _member_name(guild, user_id) -> str:
    """Display name if we can see them, primary IGN if we can't, id as last resort."""
    try:
        if guild is not None:
            m = guild.get_member(int(user_id))
            if m is not None:
                return m.display_name
    except Exception:
        pass
    try:
        ign = _db().get_ign(str(user_id))
        if ign:
            return str(ign)
    except Exception:
        pass
    return f"user {user_id}"


# ── Panel-side helper ───────────────────────────────────────────────────────
_FOOTER_RE = re.compile(r"\s*·?\s*Panel \S+(?: · /go \S+)?$")


def stamp(embed, panel_key: str, entity_id=None) -> str:
    """Print this panel's address in its footer and return it.

    Appends to whatever footer the panel already had rather than replacing it,
    and is idempotent — re-rendering the same panel does not stack codes.
    """
    code = address(panel_key, entity_id)
    tail = f"Panel {code}"
    if entity_id is not None and "." in code:
        tail += f" · /go {code.rsplit('.', 1)[-1]}"
    try:
        existing = (embed.footer.text or "") if embed.footer else ""
    except Exception:
        existing = ""
    existing = _FOOTER_RE.sub("", existing or "").rstrip(" ·")
    text = f"{existing} · {tail}" if existing else tail
    try:
        icon = embed.footer.icon_url if embed.footer else None
    except Exception:
        icon = None
    try:
        embed.set_footer(text=text[:2048], icon_url=icon)
    except Exception:
        embed.set_footer(text=text[:2048])
    return code


# ── "What can I address?" — feeds the picker so nobody types at all ────────
def suggestions_for(user_id, *, guild=None, query: str = "", limit: int = 25) -> list[dict]:
    """Addresses this user plausibly wants, newest/most relevant first.

    Returns [{token, code, kind, entity_id, label}]. Matches on the *name* as
    well as the token, so a user can pick "Greyhames" and never see a code.
    """
    ensure_schema()
    uid = str(user_id)
    q = normalise(query)
    pat = _pattern(q) if q else None
    text = (query or "").strip().lower()

    with _db().db() as conn:
        rows = conn.execute(
            "SELECT token, kind, entity_id, panel_key, panel_code FROM panel_skus "
            "ORDER BY rowid DESC LIMIT 400").fetchall()
        owned_markets = {r["market_id"] for r in conn.execute(
            "SELECT market_id FROM markets WHERE owner_id=?", (uid,)).fetchall()}
        owned_hives = {r["location"] for r in conn.execute(
            "SELECT location FROM hive_claims WHERE user_id=?", (uid,)).fetchall()}
        owned_lots = {str(r["id"]) for r in conn.execute(
            "SELECT id FROM land_listings WHERE seller_id=?", (uid,)).fetchall()}

    out, mine = [], []
    for r in rows:
        kind, eid = r["kind"], r["entity_id"]
        name = describe(kind, eid, guild=guild)
        if name is None:
            continue                       # dead entity — never offer it
        panel = PANELS.get(r["panel_key"])
        label = f"{name} — {panel.title if panel else kind}"
        if pat and not _like(r["token"], pat) and text not in name.lower():
            continue
        if text and not pat and text not in name.lower():
            continue
        item = {"token": r["token"], "kind": kind, "entity_id": eid,
                "code": f"{r['panel_code']}.{r['token']}", "label": label}
        is_mine = (eid in owned_markets or eid in owned_hives
                   or eid in owned_lots or eid == uid)
        (mine if is_mine else out).append(item)
    return (mine + out)[:limit]


def _like(token: str, pat: str) -> bool:
    if len(token) != len(pat):
        return False
    return all(p == "_" or p == t for p, t in zip(pat, token))
