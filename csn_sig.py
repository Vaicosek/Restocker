"""csn_sig — the CSN content signature. ONE definition, both ends of the wire.

WHY THIS FILE EXISTS
════════════════════
A CSN sale has no id. The plugin prints a line, we read it, and the same physical
sale can be read many times (a failed `/csn clear`, a `.seen` loss, an interrupted
walk, a re-uploaded CSV, two bot instances on one gateway event). Something has to
decide "these two readings are the same sale" and it has to decide it the SAME WAY
every time, on both sides, forever.

The old answer was `sale_uid` minted in the mod from the reconstructed timestamp
bucketed to the minute. That value is *walk-dependent*: CSN prints an age ("3h18m
ago") truncated to its finest displayed unit, so

    reconstructed = now - floor(true_age, 60s) = sale_time + (true_age mod 60s)

which lands anywhere in the half-open window [sale_time, sale_time + 60s)
depending on WHEN you walked. Flooring that to a minute yields one of TWO buckets,
so the same sale hashes to a different uid on roughly one re-read in three
(measured). Downstream, `Restocker_db.add_csn_transactions_detailed` papered over
it with a "same fields within ±90 seconds" window, which trades the duplicate for
the opposite error: two genuinely distinct identical sales 40 seconds apart
collapse into one and the second is never counted.

Ironcrest shipped the identical defect and documented it as walk-independent
(their `utils/mc_parser.py` sig is `verb|player|qty|item|amount|minute`). It is
not. This file is the version that actually is.

THE RULE
════════
The signature is a pure function of the CSV row's own fields. It contains NO
wall-clock quantity that depends on when the row was read, and no transport
metadata. Re-deriving it from a re-upload of the same data produces a
byte-identical value, and the mod and the bot compute it with the same recipe in
two languages.

Time is not in the signature. `occ` is — a small positive integer minted by the
mod that says "this is the Nth sale THIS PERIOD with exactly these fields, on this
day". Nth of the period, not of the export run: a run ends by clearing the shop's
history, so a run cannot see what earlier runs exported, and counting per run
restarted every group at 1 and re-minted yesterday's signature for today's sale.
See "WHY `occ` IS SAFE" below.

═══════════════════════════════════════════════════════════════════════════════
INCLUDED — the nine components, in this order, joined by US (0x1F)
═══════════════════════════════════════════════════════════════════════════════
  0  VERSION     literal "csnsig.v3". Bump it and every sig changes; that is the
                 point. A version bump is a deliberate re-ingest of the world.
  1  seller      shop owner IGN as CSN reported it       -> norm_name
  2  actor       counterparty IGN                        -> norm_name
  3  verb        "bought" | "sold"                       -> lowercase
  4  qty         units moved                             -> canonical int
  5  item_raw    item EXACTLY as CSN printed it,
                 INCLUDING its "#aFe" variant code       -> norm_item
  6  coins       amount, as INTEGER CENTI-COINS          -> canonical int
  7  sale_date   YYYY-MM-DD the mod attributed the sale to
  8  occ         1-based occurrence ordinal within
                 (all eight fields above) FOR THE WHOLE PERIOD,
                 minted by the mod and shipped as its own column

═══════════════════════════════════════════════════════════════════════════════
EXCLUDED — and exactly why
═══════════════════════════════════════════════════════════════════════════════
  time-of-day / timestamp_iso   Walk-dependent (see above). THIS is the bug.
  the "N ago" text              Walk-dependent by construction.
  capture / upload / receipt
    timestamps, message ids,
    webhook, channel, filename  Transport. The same sale delivered twice by two
                                routes is one sale.
  market_id                     Scope lives in the INDEX, not the hash. The mod
                                knows its configured market; the bot resolves an
                                *effective* market (channel binding, code
                                verification, typo-by-code recovery, fallback)
                                which can differ. Putting it in the hash would
                                make the two ends disagree by design. The store is
                                keyed UNIQUE(link_id, sig) instead, so the same
                                sale filed against two markets is two rows —
                                correct, because that is two different claims.
  item display name             Aliases and stock-scan profiles rewrite it over
                                time; a brew renamed in csn_profiles.json must not
                                re-ingest six weeks of its own history. The raw
                                name with its #code never changes.
  period / file name            So a re-read that lands in a LATER period file
                                still matches the row written from the earlier one.
  row order, whitespace,
    CSV quoting, §colour codes  Normalised away before hashing.

═══════════════════════════════════════════════════════════════════════════════
WHY `occ` IS SAFE — the order-independence argument
═══════════════════════════════════════════════════════════════════════════════
The mod assigns `occ` by grouping a completed walk's entries on the eight content
fields and numbering each group with CONSECUTIVE ordinals after sorting it.
Members of one group are, by definition, identical in every economic field — same
seller, buyer, verb, quantity, item and amount, on the same day. They are
interchangeable.

Therefore ANY assignment of consecutive ordinals across a group yields the SAME
MULTISET of signatures. The signature multiset is invariant under the walk order,
under CSN's page ordering (which we deliberately do not rely on knowing), and
under reconstruction drift reordering two members that fall within 60s of each
other. A re-walk regenerates the identical multiset, every member hits the unique
index, and nothing is inserted twice.

A PARTIAL walk that saw only j of the group's k members numbers them 1..j. The
next complete walk numbers all k as 1..k, of which the first j collide with what
is already stored and are correctly rejected. Self-healing, no operator action.

Two genuinely identical sales on the same day are occ=1 and occ=2: distinct
signatures, both counted. That is precisely the case Ironcrest's minute bucket
silently merges, and the case our ±90s window silently merged.

WHERE THE COUNT STARTS — and the bug that lived here
────────────────────────────────────────────────────
This section used to claim: *"Growth is monotone at the new end: a genuinely NEW
k+1'th identical sale sorts after the existing k and takes occ=k+1."* That is only
true while the walk still CONTAINS the existing k, and a successful export ends
with `/csn clear`, which wipes the shop's history. Run N+1's walk therefore cannot
see run N's sales, every group restarted at occ=1, and the second real sale of the
day re-minted the first one's signature. It was then dropped — by the mod's `.seen`
on one side, by `UNIQUE(link_id, sig)` on the other — with no row, no error and no
log line anywhere. 1 of 2 sales booked, 120.50 of 241.00, and the shop's own copy
already deleted. Every repeat purchase of the same stack at the same price, which
is the normal mode of a Minecraft shop; hive wages ride the same pipe, so a
harvester who hands in two identical loads in a day was paid for one.

The count therefore lives in a period ledger (`csn_export_<period>.occ`) that
`/csn clear` does not touch, and the ordinal only advances past an already-exported
reading on POSITIVE EVIDENCE that the reading cannot be in this walk. There are
exactly two such proofs, and both are absolute rather than heuristic:

  1. the mod's `.seen` no longer vouches for it — `.seen` is emptied only when CSN
     itself answered the post-clear read with an empty history;
  2. the reading is more than DRIFT_SECONDS newer than every committed reading of
     that content — drift is bounded and one-directional, so no reading of an
     older sale can land there.

Note the direction of proof 2. It is the exact inverse of the ±90s window this
scheme replaced: the window used PROXIMITY to declare two rows the same, which
merges distinct sales. This uses DISTANCE to declare them different. It can only
separate, never merge. With neither proof available the mod assumes re-read and
defers — the sale is still on the server, because an unconfirmed clear is not a
clear, and the next confirmed run exports it.

The bot does not re-derive any of this. `occ` arrives as its own CSV column and is
hashed as shipped, so the two ends cannot disagree about it. `assign_occurrences`
below is only for rows that arrive WITHOUT one.

═══════════════════════════════════════════════════════════════════════════════
THE ONE RESIDUAL EDGE, stated honestly
═══════════════════════════════════════════════════════════════════════════════
`sale_date` is derived from the drifting reconstruction, so a sale in the 60
seconds before midnight can be attributed to day D on one walk and D+1 on the
next. Any deterministic function of a value that can move by 60s has a boundary
somewhere; this one is parked at midnight, where it affects ~0.07% of sales
(60s / 86400s) and only when that sale is re-walked at all.

It is not left to chance. The mod ships `sale_date` as its OWN column so the bot
reads it rather than re-deriving it, and `boundary_dates()` below returns the
bounded set of dates a row could legitimately have been filed under. The store
probes those too, exactly, before deciding a row is new. That is a deterministic
two-key lookup over a 60-second band — not a fuzzy time window.

Compare: the ±90s window it replaces fired on EVERY row and could merge distinct
sales; this fires on a 60s band per day and can only ever merge two readings of
one sale.

═══════════════════════════════════════════════════════════════════════════════
INTEGER COINS
═══════════════════════════════════════════════════════════════════════════════
Amounts are canonicalised to integer centi-coins parsed from the CSV's DECIMAL
STRING with `decimal.Decimal` — never through a binary float. Java's
`Double.toString(298.13)` and Python's `repr(298.13)` are not required to agree,
and a signature that depends on a float's rendering is a signature that breaks on
a JVM upgrade. The mod writes at most 2 decimal places (BigDecimal, HALF_UP), so
x100 is always exact.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

__all__ = [
    "SIG_VERSION",
    "COIN_SCALE",
    "DRIFT_SECONDS",
    "norm_name",
    "norm_item",
    "coins_to_centi",
    "content_key",
    "sale_sig",
    "sig_for_row",
    "boundary_dates",
    "assign_occurrences",
    "SigError",
]

SIG_VERSION = "csnsig.v3"

#: Field separator. US (unit separator) cannot occur in a Minecraft chat line, so
#: no field can forge a boundary and collide with a different decomposition.
_SEP = "\x1f"

#: Coins are canonicalised to integers at this scale. The mod emits at most 2 dp.
COIN_SCALE = 100

#: The reconstruction drift bound, in seconds. CSN truncates the printed age to
#: its finest displayed unit (minutes), so a reconstructed instant is in
#: [true_sale_time, true_sale_time + 60s). Everything about the midnight edge
#: derives from this one number.
DRIFT_SECONDS = 60

#: Minecraft legacy colour codes: section sign + one character.
_COLOUR_RE = re.compile("§.", re.DOTALL)

#: Any run of whitespace collapses to one space.
_WS_RE = re.compile(r"\s+")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SigError(ValueError):
    """A row could not be canonicalised. Never swallow this — a row that cannot be
    signed cannot be deduped, and silently dropping it is how revenue disappears."""


# ── canonicalisation ────────────────────────────────────────────────────────

def _base_clean(value) -> str:
    """NFC, strip §colour codes, collapse whitespace, trim. Shared by every text
    field so the two languages have exactly one string-shaping rule to agree on."""
    s = "" if value is None else str(value)
    s = unicodedata.normalize("NFC", s)
    s = _COLOUR_RE.sub("", s)
    s = _WS_RE.sub(" ", s)
    return s.strip()


def norm_name(value) -> str:
    """Canonical form for a player name (seller, actor).

    Lower-cased: Minecraft names are [A-Za-z0-9_] and case-preserving but
    case-insensitive for identity, and servers have been observed rendering the
    same name with different capitalisation in different message templates.
    ASCII-only lowering, so Java's toLowerCase(Locale.ROOT) matches exactly —
    `str.casefold()` is deliberately NOT used because its non-ASCII foldings
    (e.g. 'ß' -> 'ss') have no Java equivalent."""
    return _base_clean(value).lower()


def norm_item(value) -> str:
    """Canonical form for the RAW item name — the one that keeps its "#aFe" code.

    NOT lower-cased: the variant code is case-significant. "Potion#akQ" and
    "Potion#akq" are, as far as anything downstream can tell, two different
    products at two different prices; folding them together would merge two
    barrels' revenue into one line."""
    return _base_clean(value)


def coins_to_centi(value) -> int:
    """Amount -> signed integer centi-coins, via Decimal, never via float.

    Accepts the CSV's decimal string ("-0.31", "298.13", "1,024.50", "+12").
    Rejects nothing silently: a value that will not parse raises, because a sale
    whose amount we cannot canonicalise is a sale we cannot sign.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value) * COIN_SCALE
    if isinstance(value, Decimal):
        dec = value
    else:
        text = ("" if value is None else str(value)).strip().replace(",", "")
        if text.startswith("+"):
            text = text[1:]
        if not text:
            raise SigError("empty coin amount")
        if isinstance(value, float):
            # A float that reached here already lost its decimal identity. Go
            # through repr, which is the shortest round-trip form, and accept the
            # 2dp quantisation below — but say so, because callers should be
            # handing us the CSV string.
            text = repr(value)
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:
            raise SigError(f"unparseable coin amount {value!r}") from exc
    if not dec.is_finite():
        raise SigError(f"non-finite coin amount {value!r}")
    scaled = (dec * COIN_SCALE).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    out = int(scaled)
    return 0 if out == 0 else out          # normalise Decimal('-0') -> 0


def _canon_int(value, field: str) -> int:
    try:
        return int(str(value).strip().replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise SigError(f"non-integer {field}: {value!r}") from exc


def _canon_date(value) -> str:
    if isinstance(value, (_date, _datetime)):
        return value.strftime("%Y-%m-%d")
    s = ("" if value is None else str(value)).strip()
    if not _DATE_RE.match(s):
        raise SigError(f"sale_date must be YYYY-MM-DD, got {value!r}")
    return s


# ── the signature ───────────────────────────────────────────────────────────

def content_key(seller, actor, verb, qty, item_raw, coins, sale_date) -> str:
    """The eight-field content identity WITHOUT the occurrence ordinal.

    This is what the mod groups on when it mints `occ`, and what the store groups
    on when it counts how many readings of one tuple it already holds. Exposed so
    both uses share the definition rather than re-implementing the join."""
    parts = (
        SIG_VERSION,
        norm_name(seller),
        norm_name(actor),
        _base_clean(verb).lower(),
        str(_canon_int(qty, "quantity")),
        norm_item(item_raw),
        str(coins_to_centi(coins)),
        _canon_date(sale_date),
    )
    return _SEP.join(parts)


def sale_sig(seller, actor, verb, qty, item_raw, coins, sale_date, occ) -> str:
    """The signature: lowercase hex SHA-256, 64 chars, of the nine components.

    Byte-identical to `CsnExportClient.saleSig(...)` in the mod. Both sides are
    exercised against the same vector table (see `_SELFTEST_VECTORS`), and the
    mod's own `--selftest` entry point prints the same table so a jar can be
    checked against a bot without a game client."""
    occ_i = _canon_int(occ, "occ")
    if occ_i < 1:
        raise SigError(f"occ must be >= 1, got {occ!r}")
    payload = content_key(seller, actor, verb, qty, item_raw, coins, sale_date) \
        + _SEP + str(occ_i)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sig_for_row(row: dict) -> str:
    """Signature for a parsed CSV row dict, as `_parse_period_transactions` yields.

    Uses `item_raw` when the mod supplied it and falls back to the display `item`
    only for pre-v3 files — noted here because that fallback is exactly the case
    where two brews that differ only by #code cannot be told apart, and the
    caller should treat such rows as legacy (see `Restocker_db.csn_ingest_record`).
    """
    item_raw = row.get("item_raw") or row.get("item") or ""
    return sale_sig(
        row.get("seller"),
        row.get("actor"),
        row.get("verb"),
        row.get("qty"),
        item_raw,
        row.get("coins_str", row.get("coins")),
        row.get("sale_date"),
        row.get("occ"),
    )


def boundary_dates(sale_date, sale_ts=None) -> list:
    """The dates this row could legitimately have been filed under, most likely first.

    Reconstruction drift is bounded and one-directional: a reconstructed instant is
    in [true_sale, true_sale + DRIFT_SECONDS). So a row whose reconstructed
    time-of-day is within the first DRIFT_SECONDS of a day may belong to the
    PREVIOUS day; a row within the last DRIFT_SECONDS of a day is safe (the drift
    can only push forward, and it already has).

    Returns [sale_date] for the overwhelming majority of rows, and a two-element
    list only inside a 60-second band per day. That bounded extra probe is what
    keeps the midnight edge from becoming a duplicate.

    `sale_ts` is the row's informational reconstructed timestamp. Without it we
    cannot tell whether the row is near the boundary, so we return the safe
    superset (both dates) rather than guess.
    """
    base = _canon_date(sale_date)
    prev = (_datetime.strptime(base, "%Y-%m-%d") - _timedelta(days=1)).strftime("%Y-%m-%d")
    if not sale_ts:
        return [base, prev]
    text = str(sale_ts).strip().replace("Z", "+00:00")
    try:
        parsed = _datetime.fromisoformat(text)
    except ValueError:
        return [base, prev]
    secs_into_day = parsed.hour * 3600 + parsed.minute * 60 + parsed.second
    if secs_into_day < DRIFT_SECONDS:
        return [base, prev]
    return [base]


# ── occurrence assignment (bot-side mirror of the mod's minting) ────────────

def assign_occurrences(rows: list) -> list:
    """Mint `occ` for rows that arrived without it (a pre-v3 mod, or a hand-made CSV).

    Group on the eight content fields, sort each group by its informational
    timestamp then by arrival index, number 1..k. Because a group's members are
    identical in every economic field the resulting MULTISET of signatures does not
    depend on the sort — see the order-independence argument in this module's
    docstring.

    Mutates and returns `rows`. Rows that already carry a positive `occ` are left
    exactly as the mod minted them: the mod counts over the whole PERIOD and holds
    the ledger that lets it, this function sees only one file, and the mod's
    numbering is the authoritative one.

    SCOPE, and why it is the file and not the database. This numbering is
    deliberately FILE-SCOPED, which is not what the mod does. It is the right
    answer here for the opposite reason: a legacy file carries no ordinal, so the
    only thing that makes re-uploading it free is that re-reading it produces
    exactly the same numbers. Seeding from what is already stored would give every
    re-upload fresh ordinals and book the whole file again — a mint, not a merge.

    The cost is stated rather than hidden: two SEPARATE legacy files, each holding
    one of two genuinely distinct identical sales, both number theirs occ=1, and
    the second is deduped away. There is no way to tell those apart from a row
    that has no ordinal and no trustworthy clock, which is precisely why the mod
    ships `occ` as its own column. Legacy rows are flagged `legacy=1` in
    `csn_ingest` so they can be found later.
    """
    groups: dict = {}
    for idx, row in enumerate(rows):
        if row.get("occ"):
            continue
        try:
            key = content_key(
                row.get("seller"), row.get("actor"), row.get("verb"), row.get("qty"),
                row.get("item_raw") or row.get("item") or "",
                row.get("coins_str", row.get("coins")), row.get("sale_date"),
            )
        except SigError:
            continue                      # unsignable; the caller reports it
        groups.setdefault(key, []).append((idx, row))

    for members in groups.values():
        members.sort(key=lambda pair: (str(pair[1].get("sale_ts") or ""), pair[0]))
        for ordinal, (_idx, row) in enumerate(members, start=1):
            row["occ"] = ordinal
    return rows


# ── cross-language agreement vectors ────────────────────────────────────────
# The mod prints this exact table under `--selftest`. If the two ever disagree,
# every sale ingested after the divergence is a duplicate, so the vectors are
# checked by `test_csn_sig.py` and are worth breaking a build over.
_SELFTEST_VECTORS = [
    # (seller, actor, verb, qty, item_raw, coins, sale_date, occ)
    ("Vaicos", "Steve", "bought", 64, "Diamond", "298.13", "2026-08-14", 1),
    ("Vaicos", "Steve", "bought", 64, "Diamond", "298.13", "2026-08-14", 2),
    ("Vaicos", "alex", "sold", 1, "Potion#akQ", "-0.31", "2026-08-14", 1),
    ("Vaicos", "alex", "sold", 1, "Potion#akq", "-0.31", "2026-08-14", 1),
    ("GreyHames", "Notch", "bought", 3456, "Honey Block", "0", "2026-01-01", 1),
    ("GreyHames", "Notch", "bought", 3456, "Honey Block", "-0", "2026-01-01", 1),
    ("Vaicos", "Bob", "bought", 2, "Oak Log", "1,024.50", "2026-12-31", 7),
]


def selftest_table() -> list:
    """[(index, sig)] for the shared vectors — compared byte-for-byte with the jar."""
    return [(i, sale_sig(*v)) for i, v in enumerate(_SELFTEST_VECTORS)]


if __name__ == "__main__":                                    # pragma: no cover
    for _i, _sig in selftest_table():
        print(f"{_i}\t{_sig}")
