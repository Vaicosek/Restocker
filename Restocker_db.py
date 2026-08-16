"""
db.py — SQLite database layer for Restocker bot.
Replaces all YAML file I/O with a single restocker.db file.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path("restocker.db")

#: Module logger. This file used bare `print` for its one status line; the CSN
#: ingest path needs real log levels, because "a row could not be signed" must be
#: findable after the fact and must not go to stdout in the middle of a run.
log = logging.getLogger("restocker.db")

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


@contextmanager
def db():
    """Context manager — yields a connection, commits on success, rolls back on error."""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def db_in(conn=None):
    """Run inside the CALLER's transaction when given one, otherwise own a new one.

    `db()` is not re-entrant: it commits on exit, over a thread-local connection.
    So a nested `with db()` inside a caller's open transaction commits the
    caller's half-written work early — which is precisely the "money moved, the
    key is recorded separately" split that took six rounds to kill in ledger v2.
    Composition therefore has to be explicit, and this is the one place it
    happens. A helper that takes `conn=` promises: if you hand me your
    transaction, my effect and your idempotency record commit together or not
    at all.
    """
    if conn is not None:
        yield conn
    else:
        with db() as c:
            yield c



SCHEMA = """
-- ── Balances ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS balances (
    user_id         TEXT PRIMARY KEY,
    coins           REAL NOT NULL DEFAULT 0,
    principal       REAL NOT NULL DEFAULT 0,
    lp              REAL NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS balance_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- ── Items ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS items (
    name            TEXT PRIMARY KEY,
    coin            REAL NOT NULL DEFAULT 0,
    stock           INTEGER NOT NULL DEFAULT 0,
    unit_type       TEXT NOT NULL DEFAULT 'pieces',
    stackable       INTEGER NOT NULL DEFAULT 1,
    stack_size      INTEGER NOT NULL DEFAULT 64,
    barrel_slots    INTEGER NOT NULL DEFAULT 54,
    market_id       TEXT NOT NULL DEFAULT 'main',
    worker_cost     REAL                            -- break-even cost (consignment futures); NULL = unset
);

-- ── Markets ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS markets (
    market_id           TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    owner_id            TEXT,
    manager_ids         TEXT NOT NULL DEFAULT '[]',   -- JSON array
    platform_fee_pct    REAL NOT NULL DEFAULT 3.0,
    csn_history_file    TEXT,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    discord_role_name   TEXT NOT NULL DEFAULT '',     -- role that identifies market leader
    leader_discord_id   TEXT,                         -- Discord user ID of current leader
    leader_code         TEXT,                         -- verification code for CSN mod
    report_channel_id   TEXT                          -- Discord channel CSN webhook posts to (routes by channel, no code needed)
);

-- ── Orders ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id                      INTEGER PRIMARY KEY,
    shop                    TEXT NOT NULL DEFAULT '',
    item                    TEXT NOT NULL,
    market_id               TEXT,
    requested               INTEGER NOT NULL DEFAULT 0,
    produced                INTEGER NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'open',
    claimed_by              TEXT,
    unit_type               TEXT NOT NULL DEFAULT 'pieces',
    amount                  INTEGER NOT NULL DEFAULT 0,
    stackable               INTEGER NOT NULL DEFAULT 1,
    stack_size              INTEGER NOT NULL DEFAULT 64,
    barrel_slots            INTEGER NOT NULL DEFAULT 54,
    coin_per_piece          REAL,
    priority_role           TEXT,
    priority_until          TEXT,
    employee_announce_at    TEXT,
    employee_announced      INTEGER NOT NULL DEFAULT 0,
    worker_announced        INTEGER NOT NULL DEFAULT 0,
    verification_ticket_id  INTEGER,
    assist_ticket_id        INTEGER,
    blocked_claimers        TEXT NOT NULL DEFAULT '[]',  -- JSON array
    messages                TEXT NOT NULL DEFAULT '{}',  -- JSON object
    assist_ticket_ids       TEXT NOT NULL DEFAULT '{}',  -- JSON object
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS order_claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL,
    user_tag    TEXT NOT NULL,
    qty         INTEGER NOT NULL DEFAULT 0,
    claimed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_order_claims_order ON order_claims(order_id);

-- ── Investors ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS investors (
    user_id         TEXT PRIMARY KEY,
    balance         REAL NOT NULL DEFAULT 0,
    principal       REAL NOT NULL DEFAULT 0,
    joined_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS investor_payout_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    amount      REAL NOT NULL,
    note        TEXT,
    paid_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Investor profit-share LEGS: one progress marker per investor ─────────────
-- investor_payout_log answers "did this month's distribution run". It could not
-- answer "which investors were paid", and the GEX.PR distribution checked the
-- MONTH tag once before its loop while writing it per investor. One investor
-- paid meant the tag existed, so every later attempt returned [] and the rest
-- were never paid again: measured 7 of 10 investors permanently unpaid, with the
-- outer `except` swallowing the reason.
--
-- Same shape as stock_dividend_legs, deliberately: a per-beneficiary row that is
-- moved to `claimed` and COMMITTED before that investor's credit is attempted,
-- and the amount pinned on the first claim so a later net correction cannot
-- re-price a leg somebody is already owed.
--
--   claimed  -- marker written, credit not yet resolved
--   applied  -- the credit returned; never pay this leg again
--   refused  -- the credit provably did not happen; re-armed on the next run
--   unknown  -- claimed by an attempt that died. MAY be paid. Never automatically
--               re-credited; a human reads the coin ledger and settles it.
CREATE TABLE IF NOT EXISTS investor_payout_claims (
    tag         TEXT NOT NULL,               -- vtech:<market_id>:<month>
    user_id     TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    state       TEXT NOT NULL DEFAULT 'claimed',
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tag, user_id),
    CHECK (state IN ('claimed','applied','refused','unknown'))
);
CREATE INDEX IF NOT EXISTS idx_investor_claims_state ON investor_payout_claims(state);

-- ── Hive Claims ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hive_claims (
    location    TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    user_tag    TEXT NOT NULL,
    claimed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Hive Pickups ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hive_batches (
    batch_id    TEXT PRIMARY KEY,
    data        TEXT NOT NULL DEFAULT '{}',  -- JSON
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS hive_active_batch (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- single row
    batch_id    TEXT
);

-- ── CSN History ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS csn_history (
    market_id   TEXT NOT NULL DEFAULT 'main',
    month       TEXT NOT NULL,            -- e.g. '2026-04'
    label       TEXT,
    source      TEXT,
    recorded_at TEXT,
    income      REAL NOT NULL DEFAULT 0,
    spent       REAL NOT NULL DEFAULT 0,
    net         REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (market_id, month)
);
-- Per-SOURCE monthly contributions. One market can be scanned by several shops, each
-- running its own CSN mod and uploading its own monthly file that covers ONLY its own
-- sales. Treating each such file as authoritative for the month made the last upload
-- win and silently discard the others — greyhames' August flip-flopped between 17,171
-- and 2,867,935 depending on which alt posted most recently. Every uploader now keeps
-- its own row here, and the month in csn_history is the SUM of them, so re-uploading a
-- file replaces that source's slice (idempotent) instead of the whole month.
CREATE TABLE IF NOT EXISTS csn_month_sources (
    market_id   TEXT NOT NULL,
    month       TEXT NOT NULL,
    source_key  TEXT NOT NULL,            -- the uploading webhook/poster, one per shop
    income      REAL NOT NULL DEFAULT 0,
    spent       REAL NOT NULL DEFAULT 0,
    items_json  TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (market_id, month, source_key)
);
-- Superseded month-source rows, kept instead of destroyed.
--
-- `csn_retire_superseded_sources` used to DELETE. Its test (items a subset, every
-- quantity <=) is also the ordinary relationship between a corner shop and a
-- flagship in the same market, so a real second shop's 40,000 coins could be
-- erased by an unrelated shop's routine upload with no archive and no undo. A
-- destructive heuristic with no undo is not a heuristic, it is a data-loss bug.
-- Retirement now MOVES the row here, so the figure is always recoverable and
-- `csn_month_totals` + the retired rows still add up to everything ever booked.
CREATE TABLE IF NOT EXISTS csn_month_sources_retired (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id     TEXT NOT NULL,
    month         TEXT NOT NULL,
    source_key    TEXT NOT NULL,
    income        REAL NOT NULL DEFAULT 0,
    spent         REAL NOT NULL DEFAULT 0,
    items_json    TEXT,
    superseded_by TEXT,                   -- the source_key that replaced it
    reason        TEXT,                   -- 'shop-stamp' | 'unstamped-rescan'
    retired_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_csn_src_retired
    ON csn_month_sources_retired(market_id, month);
CREATE TABLE IF NOT EXISTS csn_history_items (
    market_id     TEXT NOT NULL DEFAULT 'main',
    month         TEXT NOT NULL,
    item          TEXT NOT NULL,
    sold_qty      INTEGER NOT NULL DEFAULT 0,
    bought_qty    INTEGER NOT NULL DEFAULT 0,
    net_coins     REAL NOT NULL DEFAULT 0,
    -- CSN mod v1.2 detail: how many times an item transacted (velocity) and the
    -- gross split of net_coins (income = sales revenue ≥0, expense = buy spend ≤0).
    -- Enables per-item margin %, avg unit price and turnover on the ledger.
    times_sold    INTEGER NOT NULL DEFAULT 0,
    times_bought  INTEGER NOT NULL DEFAULT 0,
    income_coins  REAL NOT NULL DEFAULT 0,
    expense_coins REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (market_id, month, item)
);
CREATE INDEX IF NOT EXISTS idx_csn_items_market_month ON csn_history_items(market_id, month);

-- ── Platform Balance ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS platform_balance (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- single row
    balance     REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS platform_balance_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    month       TEXT,
    market_id   TEXT,
    amount      REAL NOT NULL,
    note        TEXT,
    logged_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Notes (note-to-self via AI agent) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id   TEXT NOT NULL,
    author_name TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Loyalty System ──────────────────────────────────────────────────────────
-- This is the shared "V Tech" pool: one balance per user, drives tiers/interest/payout
-- bonus. Stage 4 (per-market loyalty) layers market_loyalty_ledger on TOP of this table
-- rather than replacing it — every order still credits this pool (in full for V Tech-owned
-- markets, a configurable slice otherwise), so existing tiers/interest/redemptions are
-- untouched by the change.
CREATE TABLE IF NOT EXISTS loyalty (
    user_id         TEXT PRIMARY KEY,
    points          REAL NOT NULL DEFAULT 0,
    total_earned    REAL NOT NULL DEFAULT 0,
    last_activity   TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-(user, market) loyalty ledger. Each market owner sets and pays their own rewards
-- from their market's own balance — separate from the shared V Tech pool above. Two
-- markets stocking via the same worker want independent point balances, same rationale
-- as market_item_targets being per-market.
CREATE TABLE IF NOT EXISTS market_loyalty_ledger (
    user_id         TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    points          REAL NOT NULL DEFAULT 0,
    total_earned    REAL NOT NULL DEFAULT 0,
    last_activity   TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, market_id)
);
CREATE INDEX IF NOT EXISTS idx_mll_market ON market_loyalty_ledger(market_id);
CREATE INDEX IF NOT EXISTS idx_mll_user   ON market_loyalty_ledger(user_id);

-- One Discord user may register MANY in-game names (a main + alt accounts) — several
-- people run 8+ alts. So the row is keyed on `ign` (each in-game name belongs to exactly
-- ONE user, case-insensitive), NOT on user_id. The "primary" IGN for display is simply the
-- earliest-registered row for that user. CSN attribution keys off ign→user_id, so every alt
-- an owner registers automatically pools its sales/loyalty into their one Discord account.
CREATE TABLE IF NOT EXISTS ign_registry (
    ign             TEXT PRIMARY KEY COLLATE NOCASE,
    user_id         TEXT NOT NULL,
    registered_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ign_registry_user ON ign_registry(user_id);

CREATE TABLE IF NOT EXISTS ign_pending (
    user_id         TEXT PRIMARY KEY,
    dm_channel_id   TEXT,
    role_id         TEXT NOT NULL,
    guild_id        TEXT NOT NULL,
    deadline        TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Web Orders (submitted via website) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS web_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_username    TEXT NOT NULL,
    discord_id          TEXT,
    items_json          TEXT NOT NULL DEFAULT '[]',
    notes               TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    reviewed_by         TEXT,
    reviewed_at         TEXT,
    notify_msg_id       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_web_orders_status ON web_orders(status);

-- ── Futures Orders (custom item + enchant requests submitted via Discord) ───
CREATE TABLE IF NOT EXISTS futures_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    username        TEXT NOT NULL,
    item            TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    enchants        TEXT,                           -- e.g. "Fortune III, Unbreaking" or "Clean (no Silk Touch/Fortune)"
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending / approved / declined
    reviewed_by     TEXT,
    reviewed_at     TEXT,
    notify_msg_id   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_futures_orders_status ON futures_orders(status);
CREATE INDEX IF NOT EXISTS idx_futures_orders_user ON futures_orders(user_id);

-- Bulk / consignment futures — ONE order holding many line items (pasted as a text list).
-- Consignment model: the customer pays worker_cost upfront and owes (full_price - worker_cost)
-- per unit, billed as they RESELL the goods (tracked via their market's CSN sales). The price
-- columns stay NULL until priced (Stage B); Stage A captures item+qty and turns each line into
-- a real claimable work order on approval.
CREATE TABLE IF NOT EXISTS futures_bulk (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id    TEXT NOT NULL,
    customer_name  TEXT,
    market_id      TEXT,                     -- the buyer's market (where resales are tracked)
    created_by     TEXT,                     -- who set up the deal (the supplier/owner)
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending|fulfilled|declined|cancelled
    notes          TEXT,
    notify_msg_id  TEXT,
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    paid           REAL NOT NULL DEFAULT 0,   -- margin the customer has paid back so far (Stage B)
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS futures_bulk_lines (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    bulk_id        INTEGER NOT NULL,
    item           TEXT NOT NULL,
    qty            INTEGER NOT NULL DEFAULT 1,
    unit           TEXT NOT NULL DEFAULT 'pieces',   -- pieces|stacks|barrels
    enchants       TEXT,
    raw_line       TEXT,                     -- the original pasted text (for review/repair)
    item_key       TEXT,                     -- linked catalog item (for CSN resale matching, Stage B)
    worker_cost    REAL,                     -- per-unit break-even paid upfront (Stage B)
    full_price     REAL,                     -- per-unit full price (Stage B)
    sold_baseline  INTEGER NOT NULL DEFAULT 0,  -- customer's CSN cumulative sold at pricing time
    sold_qty       INTEGER NOT NULL DEFAULT 0,  -- last-computed CSN resold (cache/info, Stage B)
    sold_override  INTEGER,                  -- manual resold count; when set, overrides CSN auto
    work_order_id  INTEGER                   -- claimable order created on fulfill
);
CREATE INDEX IF NOT EXISTS idx_futures_bulk_status ON futures_bulk(status);
CREATE INDEX IF NOT EXISTS idx_futures_bulk_customer ON futures_bulk(customer_id);
CREATE INDEX IF NOT EXISTS idx_fbl_bulk ON futures_bulk_lines(bulk_id);

-- ── Per-transaction sales ledger (the CSN mod's "# PERIOD" export) ──────────
-- csn_history stores MONTHLY aggregates per item; this stores the individual sales those
-- aggregates are computed from — who bought it and exactly when. Enables daily/hourly
-- reporting and per-customer analysis, neither of which an aggregate can answer.
-- `verb` follows CSN's semantics: 'bought' = a customer bought FROM you (income, coins>0),
-- 'sold' = you bought from them (expense, coins<0).
CREATE TABLE IF NOT EXISTS csn_transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    actor       TEXT NOT NULL,             -- the other party: your customer (or supplier)
    seller      TEXT,                      -- shop owner as reported by CSN
    verb        TEXT NOT NULL,             -- 'bought' | 'sold'
    item        TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    coins       REAL NOT NULL DEFAULT 0,
    sale_ts     TEXT NOT NULL,             -- absolute ISO instant reconstructed by the mod
    sale_day    TEXT NOT NULL,             -- YYYY-MM-DD, indexed for day rollups
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_csn_txn_day    ON csn_transactions(market_id, sale_day);
CREATE INDEX IF NOT EXISTS idx_csn_txn_actor  ON csn_transactions(market_id, actor);
-- NOTE: there was a UNIQUE(market_id, actor, item, qty, coins, sale_ts) index here.
-- It is gone, and _migrate() DROPs it from existing databases. It used the
-- reconstructed `sale_ts` as the tiebreaker between two otherwise identical sales,
-- and that timestamp is minute-granular, so two real purchases in the same minute
-- collided and the second was silently discarded. Uniqueness is now the content
-- signature (uq_csn_txn_uid), decided upstream in csn_ingest.

-- ── CSN ingest: the ONE durable store every consumer reads from ─────────────
-- Every CSN sale that ever reaches this bot lands here first, exactly once, and
-- each downstream consumer then claims it with its OWN flag column.
--
-- WHY: before this table there was no single place a sale existed. The per-sale
-- ledger deduped one way (sale_uid, then a ±90s fuzzy window), the earnings
-- roll-up deduped by "whatever add_csn_transactions_detailed said was new", the
-- hive payout deduped on its own (market, ign, item, qty, sale_ts) index, and the
-- Discord report card deduped on a hash of the raw file. Five answers to one
-- question. A sale could be booked into the ledger but not paid as a wage, or
-- paid twice because one of the five said "new" while the others said "seen".
--
-- Now: `sig` (csn_sig.sale_sig — see that module for what is and is not in it) is
-- exactly reproducible from the row's own content, so INSERTING and catching the
-- UNIQUE violation IS the idempotency check. There is no read-then-write, no
-- time window, and no dependence on the mod having cleared anything: however many
-- times a sale is re-walked, re-uploaded or re-delivered, it occupies one row.
--
-- Each consumer carries its own <name>_state column so it processes each row
-- exactly once and never waits on another consumer. States are:
--   'pending'  nothing has touched it
--   'claimed'  a consumer won the claim and is acting NOW (or died mid-act)
--   'done'     the effect is committed
--   'skip'     deliberately not applicable (e.g. a non-hive item for the hive
--              consumer) — distinct from 'done' so "never applied" and "applied"
--              stay tellable apart at 2am
-- Claims are claim-first (rule 1): one atomic UPDATE ... WHERE state='pending',
-- and the effect runs only if that UPDATE matched a row. A row stuck in 'claimed'
-- is a crash mid-effect: visible, countable, re-drivable by an operator, and
-- never silently replayed.
CREATE TABLE IF NOT EXISTS csn_ingest (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id       TEXT NOT NULL,             -- effective market id: the ingest scope
    sig           TEXT NOT NULL,             -- csn_sig.SIG_VERSION content signature
    -- ── content, canonical: EXACTLY what the signature hashes ──
    -- These are normalised (names lower-cased, colour codes stripped, whitespace
    -- collapsed) because the signature has to be reproducible in two languages. They
    -- are NOT for display — see the *_display columns below.
    seller        TEXT NOT NULL DEFAULT '',
    actor         TEXT NOT NULL DEFAULT '',
    verb          TEXT NOT NULL DEFAULT '',
    item_raw      TEXT NOT NULL DEFAULT '',  -- with its #code; what the sig hashes
    -- ── the same three things as a HUMAN should see them ──
    -- Minecraft names are case-preserving, and players identify by the capitalisation
    -- they chose: JesseNapoleon, not jessenapoleon. Storing only the canonical form
    -- meant every downstream surface that reads a name out of this table — the
    -- per-customer ledger, the hive wage rows, the operator view — rendered it
    -- lower-cased. Real names over internal ones, everywhere a user looks.
    item_display  TEXT NOT NULL DEFAULT '',  -- alias/profile-resolved item name
    actor_display TEXT NOT NULL DEFAULT '',  -- counterparty IGN as CSN printed it
    seller_display TEXT NOT NULL DEFAULT '', -- shop owner IGN as CSN printed it
    qty           INTEGER NOT NULL DEFAULT 0,
    coins_centi   INTEGER NOT NULL DEFAULT 0,-- INTEGER money. No floats on this path.
    sale_date     TEXT NOT NULL DEFAULT '',  -- YYYY-MM-DD, in the signature
    occ           INTEGER NOT NULL DEFAULT 1,-- occurrence ordinal, in the signature
    -- ── informational only: NEVER inputs to the signature ──
    sale_ts       TEXT,                      -- reconstructed instant, drifts up to 60s
    source_key    TEXT,                      -- uploader, for provenance
    source_file   TEXT,
    legacy        INTEGER NOT NULL DEFAULT 0,-- 1 = pre-v3 row, occ minted bot-side
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    seen_count    INTEGER NOT NULL DEFAULT 1,-- how often it was re-delivered; pure telemetry
    -- ── per-consumer flags ──
    txn_state     TEXT NOT NULL DEFAULT 'pending',  -- csn_transactions per-sale ledger
    txn_at        TEXT,
    earn_state    TEXT NOT NULL DEFAULT 'pending',  -- csn_history / csn_history_items roll-up
    earn_at       TEXT,
    hive_state    TEXT NOT NULL DEFAULT 'pending',  -- hive wage payout
    hive_at       TEXT,
    feed_state    TEXT NOT NULL DEFAULT 'pending',  -- Discord report card / scrape feed
    feed_at       TEXT
);
-- THE dedup point. Inserting and catching the duplicate is the idempotency check.
CREATE UNIQUE INDEX IF NOT EXISTS uq_csn_ingest ON csn_ingest(link_id, sig);
-- One partial index per consumer: "what is still mine to do", cheap at any table size.
CREATE INDEX IF NOT EXISTS idx_csn_ingest_txn
    ON csn_ingest(link_id, id) WHERE txn_state = 'pending';
CREATE INDEX IF NOT EXISTS idx_csn_ingest_earn
    ON csn_ingest(link_id, id) WHERE earn_state = 'pending';
CREATE INDEX IF NOT EXISTS idx_csn_ingest_hive
    ON csn_ingest(link_id, id) WHERE hive_state = 'pending';
CREATE INDEX IF NOT EXISTS idx_csn_ingest_feed
    ON csn_ingest(link_id, id) WHERE feed_state = 'pending';
-- The midnight-boundary probe (csn_sig.boundary_dates) looks a row up by its
-- content on an adjacent date, so that lookup needs to be an index hit too.
CREATE INDEX IF NOT EXISTS idx_csn_ingest_content
    ON csn_ingest(link_id, sale_date, actor, item_raw, qty, coins_centi, verb);

-- ── Hive engine: per-player harvest feed + monthly value bookings ───────────
-- hive_harvests: one row per parsed "X sold you Nx Item" feed line. The chest shops buy
-- honey at 0 coins, so the REAL value is assigned here (unit_value snapshot) and paid out
-- by /hive payout. UNIQUE(msg_id, line_no) makes re-ingesting a Discord message a no-op.
CREATE TABLE IF NOT EXISTS hive_harvests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    ign         TEXT NOT NULL,
    user_id     TEXT,                                  -- resolved from ign_registry, NULL if unregistered
    item        TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    unit_value  REAL NOT NULL DEFAULT 0,
    wage_value  REAL NOT NULL DEFAULT 0,             -- per-piece WAGE basis; 0 = fall back to unit_value
    msg_id      TEXT NOT NULL,
    line_no     INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    paid        INTEGER NOT NULL DEFAULT 0,
    paid_at     TEXT,
    sale_ts     TEXT,                                  -- absolute ISO time of the in-game sale (from the CSN mod), NULL on legacy/untimed lines
    UNIQUE(msg_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_hive_unpaid ON hive_harvests(market_id, paid);
-- hive_ledger: accumulated monthly hive economics per market. net = value − harvester pay
-- − owner cut = V Tech's gain; the stock roll-up reads this on top of CSN months.
CREATE TABLE IF NOT EXISTS hive_ledger (
    market_id     TEXT NOT NULL,
    month         TEXT NOT NULL,                       -- YYYY-MM
    value         REAL NOT NULL DEFAULT 0,
    harvester_pay REAL NOT NULL DEFAULT 0,
    owner_pay     REAL NOT NULL DEFAULT 0,
    net           REAL NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (market_id, month)
);

-- ── Lands (claims) ledger: entries forwarded by the CSN mod's LandTracker ──
-- Every land-inbox entry (deposit/withdraw/taxes/membership) with the balance it
-- left behind. Teleport fees never appear as entries — they are INFERRED as the
-- unexplained gap between consecutive balances (see cogs/lands.py).
CREATE TABLE IF NOT EXISTS land_ledger (
    land        TEXT NOT NULL,
    entry_no    INTEGER NOT NULL,
    ts          TEXT NOT NULL,                          -- MM/DD/YYYY HH:MM as shown in-game
    kind        TEXT NOT NULL,                          -- deposit / withdraw / taxes / other
    amount      REAL NOT NULL DEFAULT 0,                -- signed effect on the balance
    new_balance REAL,                                   -- balance after this entry (NULL if not shown)
    body        TEXT,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (land, entry_no, ts)
);

CREATE TABLE IF NOT EXISTS land_balances (
    land       TEXT PRIMARY KEY,
    balance    REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Inferred teleport-fee income per land per month (recomputed idempotently from
-- land_ledger + balance snapshots — safe to rebuild any time).
CREATE TABLE IF NOT EXISTS land_fees (
    land       TEXT NOT NULL,
    month      TEXT NOT NULL,                           -- YYYY-MM
    fees       REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (land, month)
);

-- ── Stock Exchange (markets that go public, traded with server currency) ────
CREATE TABLE IF NOT EXISTS market_shares (
    market_id           TEXT PRIMARY KEY REFERENCES markets(market_id),
    active              INTEGER NOT NULL DEFAULT 1,   -- 1 = publicly tradeable, 0 = delisted
    shares_outstanding  REAL NOT NULL DEFAULT 1000,
    pe_multiplier       REAL NOT NULL DEFAULT 12,
    share_price         REAL NOT NULL DEFAULT 0,
    listed_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_priced_at      TEXT,
    last_priced_month   TEXT                          -- last csn_history month used to price this stock
);

CREATE TABLE IF NOT EXISTS stock_holdings (
    user_id     TEXT NOT NULL,
    market_id   TEXT NOT NULL REFERENCES market_shares(market_id),
    shares      REAL NOT NULL DEFAULT 0,
    cost_basis  REAL NOT NULL DEFAULT 0,               -- total coins paid for current shares (for P/L)
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, market_id)
);

CREATE INDEX IF NOT EXISTS idx_stock_holdings_market ON stock_holdings(market_id);

CREATE TABLE IF NOT EXISTS stock_trade_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    side            TEXT NOT NULL,                     -- 'buy' or 'sell'
    shares          REAL NOT NULL,
    price_per_share REAL NOT NULL,
    total_coins     REAL NOT NULL,
    traded_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_stock_trade_log_market ON stock_trade_log(market_id);
CREATE INDEX IF NOT EXISTS idx_stock_trade_log_user ON stock_trade_log(user_id);

CREATE TABLE IF NOT EXISTS stock_price_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    price       REAL NOT NULL,
    reason      TEXT,
    logged_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_stock_price_log_market ON stock_price_log(market_id);
-- ── Limit / trigger orders ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_limit_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    side            TEXT NOT NULL,
    shares          INTEGER NOT NULL,
    limit_price     REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    fill_price      REAL,
    fill_total      REAL,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    -- A refusal that is neither a fill nor a cancel used to leave no trace at
    -- all; these are where it goes. See `note_limit_order_refusal`.
    refusals        INTEGER NOT NULL DEFAULT 0,
    last_refusal    TEXT,
    last_refused_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_limit_orders_market ON stock_limit_orders(market_id, status);
CREATE INDEX IF NOT EXISTS idx_limit_orders_user ON stock_limit_orders(user_id, status);

-- ── Corporate bonds (item-collateralized debt) ──────────────────────────────
CREATE TABLE IF NOT EXISTS bonds (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id         TEXT NOT NULL,
    name              TEXT NOT NULL DEFAULT '',
    face_total        REAL NOT NULL,
    unit_price        REAL NOT NULL DEFAULT 100,
    units_total       INTEGER NOT NULL,
    units_sold        REAL NOT NULL DEFAULT 0,
    coupon_pct        REAL NOT NULL,
    term_months       INTEGER NOT NULL,
    issued_at         TEXT NOT NULL DEFAULT (datetime('now')),
    matures_at        TEXT,
    status            TEXT NOT NULL DEFAULT 'open',
    last_coupon_month TEXT,
    missed_coupons    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bonds_market ON bonds(market_id, status);
CREATE TABLE IF NOT EXISTS bond_holdings (
    bond_id   INTEGER NOT NULL,
    user_id   TEXT NOT NULL,
    units     REAL NOT NULL DEFAULT 0,
    invested  REAL NOT NULL DEFAULT 0,
    name      TEXT,
    PRIMARY KEY (bond_id, user_id)
);

-- ── Listing escrow (outside companies deposit collateral to list) ───────────
CREATE TABLE IF NOT EXISTS escrow_deposits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    party       TEXT NOT NULL,               -- who deposited (company / player name)
    kind        TEXT NOT NULL DEFAULT 'coins',  -- coins / items
    value       REAL NOT NULL,               -- coin value (items at agreed valuation)
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'held',   -- held / released / forfeited
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Shareholder voting (weight = shares + GEX.PR register share) ────────────
CREATE TABLE IF NOT EXISTS vote_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    question    TEXT NOT NULL,
    options     TEXT NOT NULL,               -- JSON array of choice labels
    created_by  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closes_at   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open' -- open / closed
);
CREATE TABLE IF NOT EXISTS vote_casts (
    proposal_id INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    choice_idx  INTEGER NOT NULL,
    weight      REAL NOT NULL DEFAULT 0,
    name        TEXT,
    cast_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (proposal_id, user_id)
);
CREATE TABLE IF NOT EXISTS investor_suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    name        TEXT,
    weight      REAL NOT NULL DEFAULT 0,     -- submitter's stake at submission time
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new', -- new / planned / done / declined
    response    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Dividend payout log ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_dividend_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT NOT NULL,
    month       TEXT NOT NULL,
    total_paid  REAL NOT NULL,
    per_share   REAL NOT NULL,
    holders     INTEGER NOT NULL,
    paid_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dividend_log_market ON stock_dividend_log(market_id);

-- ── Dividend RUNS: the per-holder progress markers ──────────────────────────
-- stock_dividend_log answers "was this month paid at all". It cannot answer
-- "which holders were paid", and that is the question a crash asks. A run killed
-- at holder 61 of 200 used to restart from holder 1: the 60 already-credited
-- holders were paid a second time and 300,000 coins were created against a
-- 1,000,000 pool, because the credit ran before the treasury debit and there was
-- no marker in between.
--
-- Shape borrowed deliberately from split_runs/split_legs rather than invented:
-- a header row pinning the plan, and one leg per beneficiary carrying its own
-- state. A leg is moved to 'claimed' and COMMITTED BEFORE its credit is
-- attempted, so a process death leaves evidence pointing at the one holder whose
-- outcome is genuinely unknown instead of losing the whole run's history.
--
-- Leg states are the three outcomes, never two:
--   planned  -- nothing attempted; safe to pay
--   applied  -- the credit returned; definitely paid, never pay again
--   unknown  -- claimed but no answer (crash/timeout). MAY be paid. Never
--               re-credited automatically; a human resolves it.
--   refused  -- the credit definitely did not happen (it raised before moving
--               coins); safe to retry on the next run
CREATE TABLE IF NOT EXISTS stock_dividend_runs (
    run_id          TEXT PRIMARY KEY,
    market_id       TEXT NOT NULL,
    month           TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'auto',   -- auto | manual
    pool            INTEGER NOT NULL DEFAULT 0,
    per_share       REAL    NOT NULL DEFAULT 0,
    holders         INTEGER NOT NULL DEFAULT 0,
    charge_treasury INTEGER NOT NULL DEFAULT 1,
    state           TEXT    NOT NULL DEFAULT 'open', -- open | complete | partial
    paid            INTEGER NOT NULL DEFAULT 0,
    treasury_charged INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    settled_at      TEXT,
    UNIQUE (market_id, month, source),
    CHECK (state IN ('open','complete','partial'))
);
CREATE INDEX IF NOT EXISTS idx_dividend_runs_state ON stock_dividend_runs(state);

CREATE TABLE IF NOT EXISTS stock_dividend_legs (
    run_id     TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    shares     REAL NOT NULL DEFAULT 0,
    amount     INTEGER NOT NULL,
    state      TEXT NOT NULL DEFAULT 'planned',
    detail     TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, user_id),
    CHECK (amount > 0),
    CHECK (state IN ('planned','claimed','applied','unknown','refused'))
);
CREATE INDEX IF NOT EXISTS idx_dividend_legs_state ON stock_dividend_legs(run_id, state);

-- ── Runtime config overrides (channel/category/guild IDs, etc.) ───────────────
CREATE TABLE IF NOT EXISTS bot_config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ── Manager teams (worker -> manager, for override commissions) ──────────────
CREATE TABLE IF NOT EXISTS team_members (
    worker_id   TEXT PRIMARY KEY,
    manager_id  TEXT NOT NULL,
    added_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_team_manager ON team_members(manager_id);

CREATE TABLE IF NOT EXISTS team_settings (
    manager_id   TEXT PRIMARY KEY,
    webhook_url  TEXT,
    channel_id   TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS team_perf_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    manager_id  TEXT NOT NULL,
    worker_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,            -- order | sales | futures | override
    coins       REAL NOT NULL DEFAULT 0,
    points      REAL NOT NULL DEFAULT 0,
    qty         INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_team_perf_mgr ON team_perf_log(manager_id);
CREATE INDEX IF NOT EXISTS idx_team_perf_created ON team_perf_log(created_at);

CREATE TABLE IF NOT EXISTS coin_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    delta         INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    reason        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_coin_ledger_user ON coin_ledger(user_id, id);

CREATE TABLE IF NOT EXISTS etf_holdings (
    user_id     TEXT PRIMARY KEY,
    units       REAL NOT NULL DEFAULT 0,
    cost_basis  REAL NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS market_stock (
    market_id   TEXT NOT NULL,
    item        TEXT NOT NULL,
    owner       TEXT,
    stock       INTEGER NOT NULL DEFAULT 0,
    capacity    INTEGER NOT NULL DEFAULT 0,
    buy_price   REAL,
    sell_price  REAL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (market_id, item)
);

-- market_stock keeps only the LATEST reading per item (ON CONFLICT DO UPDATE), so every
-- scan destroyed the previous one and no trend could ever be computed. This keeps one row
-- per item per scan-day: enough to see depletion and predict a restock, without a row per
-- scan for people who press K several times an hour.
CREATE TABLE IF NOT EXISTS market_stock_history (
    market_id  TEXT NOT NULL,
    item       TEXT NOT NULL,
    day        TEXT NOT NULL,              -- YYYY-MM-DD of the reading
    stock      INTEGER NOT NULL DEFAULT 0, -- last reading that day
    capacity   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (market_id, item, day)
);
CREATE INDEX IF NOT EXISTS idx_stock_hist ON market_stock_history(market_id, item, day);

CREATE TABLE IF NOT EXISTS stock_alarms (
    market_id   TEXT NOT NULL,
    item        TEXT NOT NULL,          -- "*" = market-wide default
    threshold   REAL NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'pct',  -- 'pct' (of capacity) or 'pieces'
    PRIMARY KEY (market_id, item)
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    funder_id   TEXT NOT NULL,
    manager_id  TEXT NOT NULL,
    budget      INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | submitted | approved | rejected | cancelled
    proof       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

CREATE TABLE IF NOT EXISTS project_members (
    project_id  INTEGER NOT NULL,
    worker_id   TEXT NOT NULL,
    share       REAL NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, worker_id)
);

-- ── Abexilas Market Index (composite of all public markets over time) ────────
CREATE TABLE IF NOT EXISTS market_index_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    total_mcap  REAL NOT NULL DEFAULT 0,
    index_value REAL NOT NULL DEFAULT 0,
    markets     INTEGER NOT NULL DEFAULT 0
);

-- ── Per-market, per-item restock targets ─────────────────────────────────────
-- How full a market owner wants to keep each item, as a % of barrel capacity, plus
-- whether that item is "tracked" (ticked) in their restock builder. Per-market by design:
-- two markets stocking the same item can want very different depths of it.
-- No row = not tracked; the market's default target applies if it's ordered anyway.
CREATE TABLE IF NOT EXISTS market_item_targets (
    market_id   TEXT NOT NULL,
    item        TEXT NOT NULL,
    target_pct  REAL NOT NULL DEFAULT 80,
    tracked     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (market_id, item)
);
CREATE INDEX IF NOT EXISTS idx_mit_market ON market_item_targets(market_id);

-- ── Land Exchange (Restocker Land Exchange — real-estate listings/auctions) ──
-- A listing is a plot of land up for sale, either fixed-price ("buy_now") or a
-- timed auction with a live current_bid/current_bidder. Escrow is NOT a separate
-- ledger here: a bidder's coins are actually deducted (core.deduct_coins) the
-- moment their bid is accepted and refunded (core.add_coins) the moment they're
-- outbid or the listing is cancelled/expired — the bidder's own `balances` row
-- IS the hold. See cogs/land_exchange.py.
CREATE TABLE IF NOT EXISTS land_listings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id           TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'item',  -- 'land' or 'item' (drives fields shown)
    title               TEXT,               -- the listing name (item name or land name)
    category            TEXT,               -- optional free-text tag (Tools / Books / Land Claims…)
    photos              TEXT,               -- JSON list of image URLs (dragged-in attachments)
    market_id           TEXT,               -- optional: company this plot backs on sale (land)
    land                TEXT,               -- optional: ties to land_ledger/land_balances
    chunks              REAL NOT NULL DEFAULT 0,
    coords              TEXT,               -- optional — seller's choice to disclose
    description         TEXT,
    image_url           TEXT,               -- optional listing image (land sells on looks)
    winner_message      TEXT,               -- seller's handover note, DM'd to the winner on close
    mode                TEXT NOT NULL DEFAULT 'auction',  -- 'fixed' or 'auction'
    quality             TEXT NOT NULL DEFAULT 'raw',
    reserve             REAL NOT NULL DEFAULT 0,   -- AI-valued or seller-set starting/reserve price
    buy_now             REAL,               -- instant-buy price (required for 'fixed')
    current_bid         REAL,
    current_bidder      TEXT,
    min_increment_pct   REAL NOT NULL DEFAULT 5.0,
    commission_pct      REAL NOT NULL DEFAULT 5.0,
    listing_fee         REAL NOT NULL DEFAULT 0,
    starts_at           TEXT NOT NULL DEFAULT (datetime('now')),
    ends_at             TEXT,               -- NULL for fixed-price (no expiry)
    anti_snipe_minutes  INTEGER NOT NULL DEFAULT 5,
    status              TEXT NOT NULL DEFAULT 'active',  -- active / sold / cancelled / expired
    channel_id          TEXT,
    message_id          TEXT,
    sold_price          REAL,
    sold_to             TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_land_listings_status ON land_listings(status);
CREATE INDEX IF NOT EXISTS idx_land_listings_seller ON land_listings(seller_id);

CREATE TABLE IF NOT EXISTS land_bids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL,
    bidder_id   TEXT NOT NULL,
    amount      REAL NOT NULL,
    ts          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_land_bids_listing ON land_bids(listing_id);

-- ── Leased parcels and their rent (LAND_ESCROW_PLAN §6 item 12, overruled) ────
-- The plan says DROP the parcel/rent domain because `cogs/lands.py` owns land
-- ownership and building a second owner is how two systems come to disagree
-- about who owns a plot. That reasoning still stands and these tables are shaped
-- to respect it: `land_leases` records a RENT AGREEMENT (who pays whom, how
-- much, how often) and nothing else. It does not record ownership, it is not
-- consulted by anything that decides ownership, and `parcel_id` is an opaque
-- string — whatever `cogs/lands.py` calls the plot. If the two ever disagree
-- about who the owner is, the lease is wrong and the parcel wins; the rent sweep
-- refuses a lease whose owner cannot be resolved rather than paying the stale one.
--
-- Rent is OFF until `realestate:rent_enabled` is set. With no leases the sweep is
-- a single indexed SELECT returning nothing.
CREATE TABLE IF NOT EXISTS land_leases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parcel_id       TEXT NOT NULL,
    tenant_id       TEXT NOT NULL,
    owner_id        TEXT NOT NULL,     -- the landlord AT THE TIME THE LEASE WAS AGREED
    amount          INTEGER NOT NULL,  -- integer coins. Rent is new money movement, so
                                       -- it starts integer and never needs a migration.
    period_days     INTEGER NOT NULL DEFAULT 30,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | ended
    last_period     TEXT,              -- the last period this parcel has PAID
    next_due_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_land_leases_due ON land_leases(status, next_due_at);

-- One row per (parcel, period). The UNIQUE index is the FIRST of the three
-- things that stop a retry charging twice — before the row claim and before the
-- ledger key, a second charge for the same month cannot even be written down.
CREATE TABLE IF NOT EXISTS land_rent_charges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id    INTEGER NOT NULL,
    parcel_id   TEXT NOT NULL,
    period      TEXT NOT NULL,          -- 'YYYY-MM' for a 30-day lease; see _rent_period
    tenant_id   TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    idem_key    TEXT NOT NULL,          -- land:parcel:<parcel_id>:rent:<period>
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|claimed|paid|failed|unknown
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    ledger_ref  TEXT,
    replayed    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_at  TEXT,
    settled_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_land_rent_period ON land_rent_charges(parcel_id, period);
CREATE UNIQUE INDEX IF NOT EXISTS idx_land_rent_idem   ON land_rent_charges(idem_key);
CREATE INDEX IF NOT EXISTS idx_land_rent_status ON land_rent_charges(status);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply safe ALTER TABLE migrations for columns added after initial schema."""
    migrations = [
        "ALTER TABLE markets ADD COLUMN discord_role_name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE markets ADD COLUMN leader_discord_id  TEXT",
        "ALTER TABLE markets ADD COLUMN leader_code        TEXT",
        "ALTER TABLE markets ADD COLUMN report_channel_id  TEXT",
        "ALTER TABLE market_shares ADD COLUMN treasury_coins REAL NOT NULL DEFAULT 0",
        "ALTER TABLE market_shares ADD COLUMN dividend_pct REAL",
        "ALTER TABLE market_shares ADD COLUMN last_dividend_month TEXT",
        # The shop-scan listing quantity ("Sell <qty> for <price>"). Stored so buy_price/
        # sell_price can be kept per-unit (= price / qty). A NULL here marks a legacy row
        # scanned before per-unit normalization existed (its price is still per-bulk and
        # not trusted for display); it self-heals on the next stock scan.
        "ALTER TABLE market_stock ADD COLUMN buy_qty  INTEGER",
        "ALTER TABLE market_stock ADD COLUMN sell_qty INTEGER",
        # Which market an order belongs to — drives per-market reward payouts and the
        # website Orders board. Older orders (pre-column) stay NULL and read as 'main'.
        "ALTER TABLE orders ADD COLUMN market_id TEXT",
        # Item category (armor / tools / swords / brews / …) — groups the shop catalog so a
        # market owner can browse and restock by section. NULL = uncategorised; the auto-
        # classifier fills these in from the item name on demand.
        "ALTER TABLE items ADD COLUMN category TEXT",
        # Consignment futures (Stage B): item break-even, per-line pricing + resale tracking,
        # and the running paid-back total on a bulk deal.
        "ALTER TABLE items ADD COLUMN worker_cost REAL",
        "ALTER TABLE futures_bulk ADD COLUMN paid REAL NOT NULL DEFAULT 0",
        "ALTER TABLE futures_bulk_lines ADD COLUMN item_key TEXT",
        "ALTER TABLE futures_bulk_lines ADD COLUMN sold_baseline INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE futures_bulk_lines ADD COLUMN sold_override INTEGER",
        # A bulk is a TOOL for filing several orders, not a separate thing to approve.
        # Each line becomes an ordinary futures order; this column is how that order
        # finds its way back to the bulk line for consignment billing on approval.
        "ALTER TABLE futures_orders ADD COLUMN bulk_line_id INTEGER",
        # Consignment has a deadline: after it passes the customer owes the FULL margin
        # whether or not the goods resold. Set on first approval, not at filing, because
        # the clock should start when the work is actually commissioned.
        "ALTER TABLE futures_bulk ADD COLUMN due_at TEXT",
        # When the upfront for this line was charged to the customer's balance. Stops a
        # re-fulfil (or a repair run) charging the same goods twice.
        "ALTER TABLE futures_bulk_lines ADD COLUMN charged_at TEXT",
        # Investors (GEX.PR preferred shareholders): display name + preferred-share count
        # from the Crimson Banking cap-table export, share_pct derived from it, and a
        # running total of profit-share coins paid out.
        "ALTER TABLE investors ADD COLUMN name TEXT",
        "ALTER TABLE investors ADD COLUMN pref_shares REAL NOT NULL DEFAULT 0",
        "ALTER TABLE investors ADD COLUMN share_pct REAL NOT NULL DEFAULT 0",
        "ALTER TABLE investors ADD COLUMN total_received REAL NOT NULL DEFAULT 0",
        # Land Exchange: listing image + seller's winner-handover message (added after
        # the table shipped, so ALTER for any DB that already created land_listings).
        "ALTER TABLE land_listings ADD COLUMN image_url TEXT",
        "ALTER TABLE land_listings ADD COLUMN winner_message TEXT",
        # Auction House generalisation: the exchange now sells items too, one command
        # (/sell) with dragged-in photos. kind/title/category/photos added after ship.
        "ALTER TABLE land_listings ADD COLUMN kind TEXT NOT NULL DEFAULT 'item'",
        "ALTER TABLE land_listings ADD COLUMN title TEXT",
        "ALTER TABLE land_listings ADD COLUMN category TEXT",
        "ALTER TABLE land_listings ADD COLUMN photos TEXT",
        # CSN per-item detail (mod v1.2): transaction counts (velocity) and the gross
        # income/expense split behind net_coins. Older rows stay 0 until re-scanned,
        # which reads as "no detail" on the ledger (margin/velocity blank, net still shown).
        "ALTER TABLE csn_history_items ADD COLUMN times_sold    INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE csn_history_items ADD COLUMN times_bought  INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE csn_history_items ADD COLUMN income_coins  REAL NOT NULL DEFAULT 0",
        "ALTER TABLE csn_history_items ADD COLUMN expense_coins REAL NOT NULL DEFAULT 0",
        # Hive harvests: absolute sale timestamp from the CSN mod, so the same sale can be
        # posted/re-scanned any number of times and still pay ONCE (see the unique index below).
        "ALTER TABLE hive_harvests ADD COLUMN sale_ts TEXT",
        # CSN per-sale stable id (mod v2.1): the mod's OWN dedup identity, shipped in the
        # export CSV's sale_uid column. Keying on it makes bot-side dedup exactly mirror
        # mod-side dedup — the old exact-sale_ts key drifted up to a minute per re-scan
        # and re-ingested the same sale as "new".
        "ALTER TABLE csn_transactions ADD COLUMN sale_uid TEXT",
        # Hive wage basis, split from sale value (2026-08-07). The shop SELLS comb at
        # 350/stack and honey at 500/stack, but harvesters are paid a percentage of a
        # LOWER internal basis (300 and 400/stack) — the spread is the company's margin.
        # One column served both jobs before, so raising a shop price also raised wages.
        # 0 means "never set" and reads as unit_value, so old rows behave exactly as before.
        "ALTER TABLE hive_harvests ADD COLUMN wage_value REAL NOT NULL DEFAULT 0",
        # The csn_sig content signature of the sale this wage row was paid for
        # (2026-08-15). Only rows that came through csn_ingest carry one; rows parsed
        # from the csn-hive WEBHOOK FEED have no signature (the feed line carries no
        # seller, verb or amount, so it cannot be signed) and stay NULL. NULL is the
        # discriminator both hive indexes below key off, so the two sources keep their
        # own dedup rule and neither weakens the other.
        "ALTER TABLE hive_harvests ADD COLUMN sale_sig TEXT",
        # csn_ingest ships with these in its CREATE TABLE, so they only matter for a
        # database built from an interim build of this change set that had the table
        # without them. Cheap, idempotent, and removes the question entirely.
        "ALTER TABLE csn_ingest ADD COLUMN actor_display  TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE csn_ingest ADD COLUMN seller_display TEXT NOT NULL DEFAULT ''",
        # ── Land Exchange escrow (LAND_ESCROW_PLAN §1.2) ──────────────────────
        # Every column here is nullable, so this is an ALTER on a live table with
        # no rewrite and no default backfill. A pre-escrow bid row reads as
        # kind=NULL/status=NULL, which `land_bid_escrow_rows` treats as "not an
        # escrow row" — it is a bid that was settled under the old debit model
        # and there is nothing to capture or release on it.
        #
        # `amount` stays REAL. Converting the float money columns is a SEPARATE
        # migration with its own dry run (land_money_migrate.py); doing it here
        # would fold two different risks into one commit and neither would be
        # testable on its own.
        "ALTER TABLE land_bids ADD COLUMN kind TEXT",             # bid | buy
        "ALTER TABLE land_bids ADD COLUMN idem_key TEXT",         # minted BEFORE the hold call
        "ALTER TABLE land_bids ADD COLUMN capture_key TEXT",      # minted at row creation, not at capture
        "ALTER TABLE land_bids ADD COLUMN hold_id TEXT",          # core's id, written back after place
        "ALTER TABLE land_bids ADD COLUMN hold_expires_at TEXT",  # mirror, for the anti-snipe extender
        # The INTEGER actually reserved at core. `amount` stays REAL and stays the
        # display figure; this is the one the capture uses, stored once so no
        # settlement ever re-derives it from a float. The two are separate on
        # purpose — `land_money_migrate.py` owns the float conversion.
        "ALTER TABLE land_bids ADD COLUMN hold_amount INTEGER",
        # DEFAULT 'legacy', not NULL: every row written before escrow existed was
        # a bid backed by a DEBIT, and it must be impossible to mistake one for
        # live escrow. `legacy` is in none of land_escrow's status sets, so no
        # sweep, release or capture will ever touch those rows.
        "ALTER TABLE land_bids ADD COLUMN status TEXT NOT NULL DEFAULT 'legacy'",
        "ALTER TABLE land_bids ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        # DEFINITE refusals of the same capture/release, so a permanently-refused
        # row parks for a human instead of being retried once a minute forever
        # (land_escrow.MAX_HOLD_REFUSALS). Distinct from `attempts`, which counts
        # every claim including the ones that worked.
        "ALTER TABLE land_bids ADD COLUMN refusals INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE land_bids ADD COLUMN last_error TEXT",
        "ALTER TABLE land_bids ADD COLUMN claimed_at TEXT",
        "ALTER TABLE land_bids ADD COLUMN settled_at TEXT",
        # UNIQUE on the key a row minted: a resumed placement that re-derives the
        # same key cannot produce a second row, whatever the caller believes.
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_land_bids_idem ON land_bids(idem_key)",
        "CREATE INDEX IF NOT EXISTS idx_land_bids_status ON land_bids(listing_id, status)",
        # Per-stage progress markers on the listing itself. The bid rows carry
        # their own state, but "has the seller been paid?" has no row of its own —
        # so it gets one column, claimed the same way (LAND_ESCROW_PLAN §2.1).
        "ALTER TABLE land_listings ADD COLUMN settle_stage TEXT",
        "ALTER TABLE land_listings ADD COLUMN settling_at  TEXT",
        "ALTER TABLE land_listings ADD COLUMN fee_stage    TEXT",
        "ALTER TABLE land_listings ADD COLUMN fee_paid     REAL",
        # ── A refused limit order must have an OUTCOME (2026-08-16) ───────────
        # `_check_limit_orders` filled on ok, cancelled on a terminal refusal,
        # and did NOTHING with `no_liquidity` / `credit_refused` / `slippage`:
        # not filled, not cancelled, not logged, retried on every price tick for
        # ever. These three columns are where the refusal goes, so the order can
        # say why it is still sitting there and can give up after a bounded
        # number of tries instead of never.
        "ALTER TABLE stock_limit_orders ADD COLUMN refusals      INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE stock_limit_orders ADD COLUMN last_refusal  TEXT",
        "ALTER TABLE stock_limit_orders ADD COLUMN last_refused_at TEXT",
        # After the ALTERs, never in the CREATE-TABLE script: on an existing
        # database that script runs first and the column would not exist yet.
        "CREATE INDEX IF NOT EXISTS idx_limit_orders_refusals "
        "ON stock_limit_orders(status, refusals)",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass

    # Hive dedup, now split by SOURCE, because the two sources can answer "is this the
    # same sale?" with very different confidence.
    #
    # 1. WEBHOOK-FEED rows (sale_sig IS NULL) keep the original identity index exactly
    #    as it was: market+ign+item+qty+sale_ts, timed rows only. A feed line has no
    #    seller, verb or coin amount, so it cannot be signed and there is nothing
    #    better available for it.
    #
    #    The predicate gains `AND sale_sig IS NULL` so it stops applying to rows that
    #    DO carry a signature. That matters because sale_ts is reconstructed from
    #    "Xh Ym ago" at MINUTE precision: two genuinely separate harvests of 64 honey
    #    by the same player inside one minute reconstruct to the SAME sale_ts, collided
    #    on this index, and the second was silently dropped by the INSERT OR IGNORE —
    #    a harvester simply never got paid for it, with no error anywhere. Rows with a
    #    signature can tell those two apart exactly, so they must not be judged by an
    #    index that cannot.
    try:
        conn.execute("DROP INDEX IF EXISTS uq_hive_sale")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_hive_sale "
                     "ON hive_harvests(market_id, ign, item, qty, sale_ts) "
                     "WHERE sale_ts IS NOT NULL AND sale_sig IS NULL")
    except sqlite3.OperationalError:
        pass

    # 2. CSN-EXPORT rows (sale_sig IS NOT NULL) are keyed on the signature alone.
    #
    #    NOT market-scoped, deliberately, and that is a behaviour we are KEEPING: a
    #    sale is a physical event — the same honey leaving the same barrel — and the
    #    market id is only how the exporter happened to be configured at the time.
    #    When the same shop was exported under two market ids, the identical sale was
    #    recorded once per market and EACH market paid the harvester (observed live:
    #    JesseNapoleon's Honey Block sales under both 'greyhames' and 'vtech'). The
    #    ±120s heuristic in add_hive_harvest was the previous answer to that; this is
    #    the exact one. First market to record the sale owns it.
    #
    #    Note the deliberate asymmetry with csn_ingest, which is keyed
    #    UNIQUE(link_id, sig) and DOES store the same sale under two markets. That is
    #    correct at that layer: two markets each making a claim is two claims worth
    #    recording. But a wage is paid once to one person, so the wage ledger collapses
    #    them. Each layer keys on what it is actually counting.
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_hive_sig "
                     "ON hive_harvests(sale_sig) WHERE sale_sig IS NOT NULL")
    except sqlite3.OperationalError:
        pass

    # CSN transactions: one row per (market, sale_uid). sale_uid now carries the
    # csn_sig content signature, which is exactly reproducible, so this index is the
    # only per-sale uniqueness constraint the table needs.
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_csn_txn_uid "
                     "ON csn_transactions(market_id, sale_uid) "
                     "WHERE sale_uid IS NOT NULL")
    except sqlite3.OperationalError:
        pass

    # DROP uq_csn_txn — it silently merged genuinely distinct sales.
    #
    # The index was UNIQUE(market_id, actor, item, qty, coins, sale_ts) — that is,
    # it made `sale_ts` the tiebreaker between two otherwise identical sales. But
    # sale_ts is the ONE field that is not trustworthy: it is reconstructed from
    # CSN's "Xm ago" text at minute granularity, so two genuinely separate
    # purchases of the same item by the same player in the same minute reconstruct
    # to the SAME instant, collide on this index, and the second one is thrown away
    # by the `INSERT OR IGNORE`. Silently: no error, no log line, the coins simply
    # never appear.
    #
    # Caught by test_csn_ingest.py check 9 — two identical 120.50 sales landed
    # correctly as two rows in csn_ingest and then collapsed to one 120.50 row in
    # csn_transactions, so the ledger read 120.50 where the shop had earned 241.00.
    #
    # It is safe to drop because dedup no longer lives here: every row now arrives
    # via csn_ingest, which has already decided uniqueness on the content signature,
    # and uq_csn_txn_uid enforces one ledger row per signature. Keeping both meant a
    # correct upstream decision being overruled by a broken downstream one.
    try:
        conn.execute("DROP INDEX IF EXISTS uq_csn_txn")
    except sqlite3.OperationalError:
        pass

    # CSN history: upgrade the legacy single-market table (month PRIMARY KEY,
    # no market_id) to the market-aware schema, preserving any rows as 'main'.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(csn_history)").fetchall()]
        if cols and "market_id" not in cols:
            conn.execute("ALTER TABLE csn_history RENAME TO _csn_history_legacy")
            conn.execute(
                "CREATE TABLE csn_history ("
                "market_id TEXT NOT NULL DEFAULT 'main', month TEXT NOT NULL, label TEXT, "
                "source TEXT, recorded_at TEXT, income REAL NOT NULL DEFAULT 0, "
                "spent REAL NOT NULL DEFAULT 0, net REAL NOT NULL DEFAULT 0, "
                "PRIMARY KEY (market_id, month))"
            )
            conn.execute(
                "INSERT INTO csn_history (market_id, month, label, income, spent, net) "
                "SELECT 'main', month, label, income, spent, net FROM _csn_history_legacy"
            )
            conn.execute("DROP TABLE _csn_history_legacy")
    except sqlite3.OperationalError:
        pass

    # IGN registry: upgrade the legacy one-IGN-per-user table (user_id PRIMARY KEY) to the
    # multi-IGN shape (ign PRIMARY KEY, user_id a plain indexed column) so one Discord user
    # can own several in-game names (main + alts). Each old row — a user's single IGN —
    # carries over unchanged and stays that user's primary (earliest-registered).
    try:
        info = conn.execute("PRAGMA table_info(ign_registry)").fetchall()
        user_id_is_pk = any(r[1] == "user_id" and r[5] == 1 for r in info)
        if info and user_id_is_pk:
            conn.execute("ALTER TABLE ign_registry RENAME TO _ign_registry_legacy")
            conn.execute(
                "CREATE TABLE ign_registry ("
                "ign TEXT PRIMARY KEY COLLATE NOCASE, user_id TEXT NOT NULL, "
                "registered_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            conn.execute(
                "INSERT OR IGNORE INTO ign_registry (ign, user_id, registered_at) "
                "SELECT ign, user_id, registered_at FROM _ign_registry_legacy"
            )
            conn.execute("DROP TABLE _ign_registry_legacy")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ign_registry_user ON ign_registry(user_id)")
    except sqlite3.OperationalError:
        pass


def init_db():
    """Create all tables if they don't exist, then run migrations."""
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute("INSERT OR IGNORE INTO platform_balance (id, balance) VALUES (1, 0)")
        conn.execute("INSERT OR IGNORE INTO hive_active_batch (id, batch_id) VALUES (1, NULL)")
    # Subsystems that own their own DDL. They bootstrap themselves lazily anyway
    # (ensure_schema() is idempotent), so this is only to have the tables in place
    # before the first panel renders. Imported here rather than at module scope:
    # both modules import THIS one back for db(), and a top-level import would be a
    # cycle. A build without these files still boots.
    for _mod in ("panel_skus", "action_log"):
        try:
            __import__(_mod).ensure_schema()
        except Exception as _e:
            print(f"⚠️ {_mod}: schema bootstrap skipped ({_e})")
    print("✅ Database initialised.")



def get_balance(user_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM balances WHERE user_id=?", (str(user_id),)).fetchone()
        if row:
            return dict(row)
        return {"user_id": str(user_id), "coins": 0, "principal": 0, "lp": 0}


def set_balance(user_id: str, coins: float, principal: float = None, lp: float = None):
    with db() as conn:
        existing = conn.execute("SELECT * FROM balances WHERE user_id=?", (str(user_id),)).fetchone()
        if existing:
            p = principal if principal is not None else existing["principal"]
            l = lp if lp is not None else existing["lp"]
        else:
            p = principal if principal is not None else 0
            l = lp if lp is not None else 0
        conn.execute("""
            INSERT INTO balances (user_id, coins, principal, lp, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                coins=excluded.coins,
                principal=excluded.principal,
                lp=excluded.lp,
                updated_at=excluded.updated_at
        """, (str(user_id), coins, p, l))


def adjust_balance_tx(conn, user_id: str, delta: int, *,
                      counts_as_principal: bool = True,
                      reduce_principal: bool = True,
                      reason: Optional[str] = None) -> tuple[int, int, int]:
    """`adjust_balance`, but INSIDE the caller's transaction — and, when `reason`
    is given, the coin_ledger row is one more statement in that same transaction.

    WHY THIS EXISTS (money review §6)
    ---------------------------------
    `adjust_balance` moved the coins and `record_coin_ledger` wrote the tag in a
    SECOND `with db()` block, documented "best-effort: never raises" and wrapped
    in `except Exception: pass`. Two commits, no atomicity. Any caller that
    promotes that ledger row from an *audit log* to a *correctness guarantee* —
    which `action_log`'s Retry path does, because `coin_ledger_has(uid, key)` is
    the only thing standing between a retry and a second refund — inherits a
    window where the money has moved and the key is absent. A crash, or a
    SQLITE_BUSY swallowed by that `except`, and the retry pays again.

    This is the same defect ledger v2 spent six rounds on, and this is ledger
    v2's answer, not a second one: `ledger_v2._finalize_idempotency` records the
    key with "one more statement in the same transaction as the debit and the
    credit; they commit together or not at all". Here the ledger INSERT is that
    statement. `reason` present and no ledger row is now an impossible state.

    Deliberately NOT swallowing: the INSERT is allowed to raise. A caller that
    passes a namespaced idempotency reason (`rb:<action>#<op>`) is protected by
    the partial UNIQUE index on `coin_ledger(user_id, reason)` — a duplicate
    raises IntegrityError and takes the balance delta down with it, which is
    exactly the outcome wanted. `record_coin_ledger` keeps its old best-effort
    behaviour for the callers that only want an audit line.

    Returns (coins_after, principal_after, applied_delta); `applied_delta` is
    read inside the transaction, so it is the real change to THIS wallet by THIS
    statement and cannot be corrupted by concurrent activity the way a
    before/after pair of separate reads can.
    """
    uid = str(user_id)
    d = int(delta or 0)
    conn.execute(
        "INSERT INTO balances (user_id, coins, principal, lp) VALUES (?, 0, 0, 0) "
        "ON CONFLICT(user_id) DO NOTHING", (uid,))
    before = conn.execute("SELECT coins FROM balances WHERE user_id=?", (uid,)).fetchone()
    old_coins = int(before["coins"]) if before else 0
    if d > 0:
        conn.execute(
            "UPDATE balances SET coins = coins + ?, principal = principal + ?, "
            "updated_at = datetime('now') WHERE user_id = ?",
            (d, d if counts_as_principal else 0, uid))
    elif d < 0:
        amt = -d
        # RHS expressions are evaluated against the pre-update row, so `coins`
        # here is the balance before deduction -> MIN(amt, coins) is the amount
        # actually removed, matching the old read-modify-write semantics exactly.
        conn.execute(
            "UPDATE balances SET "
            "principal = CASE WHEN ? THEN MAX(0, principal - MIN(principal, MIN(?, coins))) "
            "ELSE principal END, "
            "coins = MAX(0, coins - ?), "
            "updated_at = datetime('now') WHERE user_id = ?",
            (1 if reduce_principal else 0, amt, amt, uid))
    row = conn.execute("SELECT coins, principal FROM balances WHERE user_id=?", (uid,)).fetchone()
    coins = int(row["coins"])
    principal = int(row["principal"])
    if reason is not None:
        conn.execute(
            "INSERT INTO coin_ledger (user_id, delta, balance_after, reason) VALUES (?,?,?,?)",
            (uid, coins - old_coins, coins, str(reason)[:200]))
    return coins, principal, coins - old_coins


def adjust_balance(user_id: str, delta: int, *, counts_as_principal: bool = True,
                   reduce_principal: bool = True) -> tuple[int, int, int]:
    """Atomically apply an integer coin delta in a single transaction (no
    read-modify-write race between concurrent coin operations).

    delta > 0 adds coins (and grows principal iff counts_as_principal).
    delta < 0 deducts, clamped at 0 (and reduces principal by the amount actually
    removed iff reduce_principal).

    Returns (coins_after, principal_after, applied_delta) where applied_delta is the
    real change to coins (may be smaller in magnitude than `delta` when clamped).

    Writes no ledger row — every existing caller pairs this with its own
    `record_coin_ledger`. A caller that needs the tag and the money to commit
    together calls `adjust_balance_tx(conn, ..., reason=key)` instead."""
    with db() as conn:
        return adjust_balance_tx(conn, user_id, delta,
                                 counts_as_principal=counts_as_principal,
                                 reduce_principal=reduce_principal)


def get_all_balances() -> dict:
    """Return {user_id: coins} dict for backward compatibility."""
    with db() as conn:
        rows = conn.execute("SELECT user_id, coins FROM balances").fetchall()
        return {row["user_id"]: row["coins"] for row in rows}


def record_coin_ledger(user_id: str, delta: int, balance_after: int, reason: str = "") -> None:
    """Append one coin movement to the audit ledger. Best-effort: never raises."""
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO coin_ledger (user_id, delta, balance_after, reason) VALUES (?,?,?,?)",
                (str(user_id), int(delta), int(balance_after), (reason or "")[:200]))
    except Exception:
        pass


def coin_ledger_has(user_id: str, reason: str) -> bool:
    """True if this exact (user, reason) coin movement is already on record.

    Used to make retroactive repairs idempotent: a repair tags its payout with
    `repair:order#N`, so re-running the repair can look here and refuse to pay twice.
    Fails CLOSED (returns True) on error — if we can't verify, we must not pay again."""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT 1 FROM coin_ledger WHERE user_id=? AND reason=? LIMIT 1",
                (str(user_id), str(reason))).fetchone()
            return row is not None
    except Exception:
        return True


def get_coin_ledger(user_id: str, limit: int = 20) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT delta, balance_after, reason, created_at FROM coin_ledger "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?", (str(user_id), int(limit))).fetchall()
        return [dict(r) for r in rows]


def backup_database(dest_path) -> str:
    """Make a consistent online snapshot of the live DB (safe with WAL) to dest_path.
    Returns the destination path. Uses sqlite3's backup API."""
    import sqlite3 as _sq
    src = _get_conn()
    dest = _sq.connect(str(dest_path))
    try:
        with dest:
            src.backup(dest)
    finally:
        dest.close()
    return str(dest_path)


def get_balance_meta() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM balance_meta").fetchall()
        return {row["key"]: row["value"] for row in rows}


def set_balance_meta(key: str, value: str):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO balance_meta (key, value) VALUES (?,?)", (key, value))



def get_items(market_id: str = None) -> dict:
    with db() as conn:
        if market_id:
            rows = conn.execute("SELECT * FROM items WHERE market_id=?", (market_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM items").fetchall()
        return {row["name"]: dict(row) for row in rows}


def get_item(name: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM items WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None


def set_item_category(name: str, category: str) -> None:
    """Tag an item with a category (armor / tools / swords / …)."""
    with db() as conn:
        conn.execute("UPDATE items SET category=? WHERE name=?",
                     ((category or "").strip() or None, str(name)))


def get_market_item_targets(market_id: str) -> dict:
    """{item: {'target_pct': float, 'tracked': bool}} for one market. Empty = nothing set up."""
    with db() as conn:
        rows = conn.execute(
            "SELECT item, target_pct, tracked FROM market_item_targets WHERE market_id=?",
            (str(market_id),)).fetchall()
        return {r["item"]: {"target_pct": float(r["target_pct"] or 0),
                            "tracked": bool(r["tracked"])} for r in rows}


def set_market_item_target(market_id: str, item: str, target_pct: float = None,
                           tracked: bool = None) -> None:
    """Upsert one item's restock target for a market. Either field may be omitted to leave
    it untouched — ticking a box shouldn't silently reset a % the owner already tuned."""
    with db() as conn:
        cur = conn.execute(
            "SELECT target_pct, tracked FROM market_item_targets WHERE market_id=? AND item=?",
            (str(market_id), str(item))).fetchone()
        old_pct = float(cur["target_pct"]) if cur else 80.0
        old_trk = bool(cur["tracked"]) if cur else True
        new_pct = old_pct if target_pct is None else max(0.0, min(100.0, float(target_pct)))
        new_trk = old_trk if tracked is None else bool(tracked)
        conn.execute("""
            INSERT INTO market_item_targets (market_id, item, target_pct, tracked, updated_at)
            VALUES (?,?,?,?, datetime('now'))
            ON CONFLICT(market_id, item) DO UPDATE SET
                target_pct=excluded.target_pct, tracked=excluded.tracked,
                updated_at=datetime('now')
        """, (str(market_id), str(item), new_pct, int(new_trk)))


def clear_market_item_target(market_id: str, item: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM market_item_targets WHERE market_id=? AND item=?",
                     (str(market_id), str(item)))


def upsert_item(name: str, coin: float, stock: int, **kwargs):
    with db() as conn:
        conn.execute("""
            INSERT INTO items (name, coin, stock, unit_type, stackable, stack_size, barrel_slots, market_id)
            VALUES (:name, :coin, :stock, :unit_type, :stackable, :stack_size, :barrel_slots, :market_id)
            ON CONFLICT(name) DO UPDATE SET
                coin=excluded.coin, stock=excluded.stock,
                unit_type=excluded.unit_type, stackable=excluded.stackable,
                stack_size=excluded.stack_size, barrel_slots=excluded.barrel_slots
        """, {
            "name": name, "coin": coin, "stock": stock,
            "unit_type": kwargs.get("unit_type", "pieces"),
            "stackable": int(kwargs.get("stackable", True)),
            "stack_size": kwargs.get("stack_size", 64),
            "barrel_slots": kwargs.get("barrel_slots", 54),
            "market_id": kwargs.get("market_id", "main"),
        })


def update_item_stock(name: str, stock: int):
    with db() as conn:
        conn.execute("UPDATE items SET stock=? WHERE name=?", (stock, name))


def delete_item(name: str) -> bool:
    """Remove an item from the catalog. Returns True if a row was deleted."""
    with db() as conn:
        cur = conn.execute("DELETE FROM items WHERE name=?", (name,))
        return cur.rowcount > 0


def rename_item(old_name: str, new_name: str):
    with db() as conn:
        conn.execute("UPDATE items SET name=? WHERE name=?", (new_name, old_name))
        conn.execute("UPDATE orders SET item=? WHERE item=?", (new_name, old_name))



def get_markets() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT * FROM markets").fetchall()
        result = {}
        for row in rows:
            d = dict(row)
            d["manager_ids"] = json.loads(d["manager_ids"])
            result[d["market_id"]] = d
        return result


def upsert_market(market_id: str, name: str, **kwargs):
    with db() as conn:
        conn.execute("""
            INSERT INTO markets (
                market_id, name, owner_id, manager_ids, platform_fee_pct,
                csn_history_file, active, created_at,
                discord_role_name, leader_discord_id, leader_code, report_channel_id
            )
            VALUES (
                :mid, :name, :owner, :mgrs, :fee,
                :csn, :active, :created,
                :role_name, :leader_id, :leader_code, :report_channel_id
            )
            ON CONFLICT(market_id) DO UPDATE SET
                name=excluded.name, owner_id=excluded.owner_id,
                manager_ids=excluded.manager_ids, platform_fee_pct=excluded.platform_fee_pct,
                active=excluded.active,
                discord_role_name=excluded.discord_role_name,
                -- only overwrite leader / channel fields when a new value is supplied,
                -- so unrelated market edits never wipe an existing value
                leader_discord_id=COALESCE(excluded.leader_discord_id, markets.leader_discord_id),
                leader_code=COALESCE(excluded.leader_code, markets.leader_code),
                report_channel_id=COALESCE(excluded.report_channel_id, markets.report_channel_id)
        """, {
            "mid":         market_id,
            "name":        name,
            "owner":       kwargs.get("owner_id"),
            "mgrs":        json.dumps(kwargs.get("manager_ids", [])),
            "fee":         kwargs.get("platform_fee_pct", 3.0),
            "csn":         kwargs.get("csn_history_file"),
            "active":      int(kwargs.get("active", True)),
            "created":     kwargs.get("created_at", datetime.now(timezone.utc).isoformat()),
            "role_name":   kwargs.get("discord_role_name", ""),
            "leader_id":   kwargs.get("leader_discord_id"),
            "leader_code": kwargs.get("leader_code"),
            "report_channel_id": (
                str(kwargs["report_channel_id"])
                if kwargs.get("report_channel_id") else None
            ),
        })


def delete_market(market_id: str) -> dict:
    """Delete a market and its per-market stock, stock alarms, and share listing. Sales
    history and orders are intentionally left intact (audit trail). Returns a dict of how
    many rows were removed from each table, e.g. {'markets':1,'market_stock':0,...}."""
    counts = {}
    with db() as conn:
        # AUDIT FIX: stock_holdings and stock_limit_orders used to survive deletion —
        # stale holders inherited free shares of any future market reusing the id, and
        # old limit orders stayed armed. (The delete COMMAND refuses to run while real
        # holders exist — delist first — so by the time this runs these are remnants.)
        for tbl in ("market_stock", "stock_alarms", "market_shares",
                    "stock_holdings", "stock_limit_orders"):
            try:
                cur = conn.execute(f"DELETE FROM {tbl} WHERE market_id=?", (str(market_id),))
                counts[tbl] = cur.rowcount
            except Exception:
                counts[tbl] = 0
        cur = conn.execute("DELETE FROM markets WHERE market_id=?", (str(market_id),))
        counts["markets"] = cur.rowcount
    return counts


def set_market_report_channel(market_id: str, channel_id) -> None:
    """Bind (or clear, with channel_id=None) a market's CSN report channel WITHOUT
    touching any other market field. upsert_market overwrites owner/managers/fee on
    conflict, so it must NOT be used just to set the channel binding."""
    with db() as conn:
        conn.execute(
            "UPDATE markets SET report_channel_id=? WHERE market_id=?",
            (str(channel_id) if channel_id else None, str(market_id)),
        )


def get_market_by_channel(channel_id) -> Optional[dict]:
    """Return the market dict bound to this Discord channel, or None.

    Channel binding lets CSN webhook reports route to the right market by the
    channel they post in — no in-game verification code required.
    """
    if not channel_id:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM markets WHERE report_channel_id = ?",
            (str(channel_id),),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["manager_ids"] = json.loads(d["manager_ids"])
        except Exception:
            d["manager_ids"] = []
        return d



def load_orders() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
        orders = []
        for row in rows:
            o = dict(row)
            o["messages"] = json.loads(o["messages"])
            o["blocked_claimers"] = json.loads(o["blocked_claimers"])
            o["assist_ticket_ids"] = json.loads(o["assist_ticket_ids"])
            claims = conn.execute(
                "SELECT * FROM order_claims WHERE order_id=? ORDER BY claimed_at", (o["id"],)
            ).fetchall()
            o["claims"] = [dict(c) for c in claims]
            orders.append(o)
        return orders


def get_order(order_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            return None
        o = dict(row)
        o["messages"] = json.loads(o["messages"])
        o["blocked_claimers"] = json.loads(o["blocked_claimers"])
        o["assist_ticket_ids"] = json.loads(o["assist_ticket_ids"])
        claims = conn.execute(
            "SELECT * FROM order_claims WHERE order_id=? ORDER BY claimed_at", (order_id,)
        ).fetchall()
        o["claims"] = [dict(c) for c in claims]
        return o


def save_order(order: dict):
    """Insert or update an order dict (same shape as the old YAML format)."""
    with db() as conn:
        conn.execute("""
            INSERT INTO orders (
                id, shop, item, market_id, requested, produced, status, claimed_by,
                unit_type, amount, stackable, stack_size, barrel_slots,
                coin_per_piece, priority_role, priority_until,
                employee_announce_at, employee_announced, worker_announced,
                verification_ticket_id, assist_ticket_id,
                blocked_claimers, messages, assist_ticket_ids,
                created_at, updated_at
            ) VALUES (
                :id, :shop, :item, :market_id, :requested, :produced, :status, :claimed_by,
                :unit_type, :amount, :stackable, :stack_size, :barrel_slots,
                :coin_per_piece, :priority_role, :priority_until,
                :employee_announce_at, :employee_announced, :worker_announced,
                :verification_ticket_id, :assist_ticket_id,
                :blocked_claimers, :messages, :assist_ticket_ids,
                :created_at, :updated_at
            )
            ON CONFLICT(id) DO UPDATE SET
                shop=excluded.shop, item=excluded.item, market_id=excluded.market_id,
                requested=excluded.requested, produced=excluded.produced,
                status=excluded.status, claimed_by=excluded.claimed_by,
                unit_type=excluded.unit_type, amount=excluded.amount,
                stackable=excluded.stackable, stack_size=excluded.stack_size,
                barrel_slots=excluded.barrel_slots,
                coin_per_piece=excluded.coin_per_piece,
                priority_role=excluded.priority_role,
                priority_until=excluded.priority_until,
                employee_announce_at=excluded.employee_announce_at,
                employee_announced=excluded.employee_announced,
                worker_announced=excluded.worker_announced,
                verification_ticket_id=excluded.verification_ticket_id,
                assist_ticket_id=excluded.assist_ticket_id,
                blocked_claimers=excluded.blocked_claimers,
                messages=excluded.messages,
                assist_ticket_ids=excluded.assist_ticket_ids,
                updated_at=datetime('now')
        """, {
            "id": order.get("id"),
            "shop": order.get("shop", ""),
            "item": order.get("item", ""),
            "market_id": order.get("market_id"),
            "requested": order.get("requested", 0),
            "produced": order.get("produced", 0),
            "status": order.get("status", "open"),
            "claimed_by": order.get("claimed_by"),
            "unit_type": order.get("unit_type", "pieces"),
            "amount": order.get("amount", 0),
            "stackable": int(order.get("stackable", True)),
            "stack_size": order.get("stack_size", 64),
            "barrel_slots": order.get("barrel_slots", 54),
            "coin_per_piece": order.get("coin_per_piece"),
            "priority_role": order.get("priority_role"),
            "priority_until": order.get("priority_until"),
            "employee_announce_at": order.get("employee_announce_at"),
            "employee_announced": int(order.get("employee_announced", False)),
            "worker_announced": int(order.get("worker_announced", False)),
            "verification_ticket_id": order.get("verification_ticket_id"),
            "assist_ticket_id": order.get("assist_ticket_id"),
            "blocked_claimers": json.dumps(order.get("blocked_claimers", [])),
            "messages": json.dumps(order.get("messages", {})),
            "assist_ticket_ids": json.dumps(order.get("assist_ticket_ids", {})),
            "created_at": order.get("created_at", datetime.now(timezone.utc).isoformat()),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        if "claims" in order:
            conn.execute("DELETE FROM order_claims WHERE order_id=?", (order["id"],))
            for c in order["claims"]:
                conn.execute("""
                    INSERT INTO order_claims (order_id, user_id, user_tag, qty, claimed_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (order["id"], str(c.get("user_id", "")), c.get("user_tag", ""),
                      c.get("qty", 0), c.get("claimed_at", datetime.now(timezone.utc).isoformat())))


def next_order_id() -> int:
    with db() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS nid FROM orders").fetchone()
        return row["nid"]



def get_investors() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT * FROM investors").fetchall()
        return {row["user_id"]: dict(row) for row in rows}


def get_investor(user_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM investors WHERE user_id=?", (str(user_id),)).fetchone()
        return dict(row) if row else None


def upsert_investor(user_id: str, balance: float, principal: float, joined_at: str = None):
    with db() as conn:
        conn.execute("""
            INSERT INTO investors (user_id, balance, principal, joined_at, updated_at)
            VALUES (?, ?, ?, COALESCE(?, datetime('now')), datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                balance=excluded.balance, principal=excluded.principal, updated_at=datetime('now')
        """, (str(user_id), balance, principal, joined_at))


def add_investor_payout(user_id: str, amount: float, note: str = None, *, conn=None):
    """Record the receipt for an investor credit. Pass `conn=` to write it in the
    same transaction as the credit itself (see `db_in`)."""
    with db_in(conn) as conn:
        conn.execute(
            "INSERT INTO investor_payout_log (user_id, amount, note) VALUES (?,?,?)",
            (str(user_id), amount, note)
        )
        conn.execute("UPDATE investors SET total_received = total_received + ?, "
                     "updated_at = datetime('now') WHERE user_id=?",
                     (float(amount), str(user_id)))


def get_investor_payout_log(limit: int = 50) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM investor_payout_log ORDER BY paid_at DESC LIMIT ?",
                            (int(limit),)).fetchall()
        return [dict(r) for r in rows]


def investor_payout_exists(note: str) -> bool:
    """True if a distribution with this note tag already ran.

    NOT the idempotency guard for the monthly V Tech profit share any more. It was,
    and it was checked ONCE before the payout loop while the tag was written per
    investor — so the first investor paid made the tag exist and every subsequent
    attempt returned early with the other nine unpaid, permanently. The guard is
    now per-investor (`investor_leg_claim`), checked inside the loop. This stays
    for display/audit callers asking "did anything at all happen for this tag".
    """
    with db() as conn:
        return conn.execute("SELECT 1 FROM investor_payout_log WHERE note=? LIMIT 1",
                            (str(note),)).fetchone() is not None


def investor_leg_claim(tag: str, user_id: str, amount: int, *, conn=None) -> int:
    """Claim-first, per investor: write this investor's marker as the FIRST
    statement of the transaction that pays them. Returns the amount we are
    cleared to pay, or 0 if we did not win the row.

    It used to say "and COMMIT it before any coins move", and that was the best
    available answer while the marker and the credit were separate transactions:
    a death then cost one investor their CERTAINTY rather than their coins. Pass
    `conn=` — as `_distribute_investor_profit` now does — and the marker, the
    credit and the receipt commit together, so a death rolls the leg back to
    unclaimed (or to `refused`, if that is where it came from) and the next run
    simply pays it. Same move `_execute_dividend_run` made per holder leg.

    One atomic write gated on the believed state, and the ROWCOUNT decides — never
    a preceding SELECT:
      * no row yet          -> INSERT OR IGNORE wins it, state `claimed`
      * row is `refused`    -> a previous attempt provably paid nothing, so it is
                               re-armed to `claimed` and paid again
      * `claimed`/`applied`/`unknown` -> 0. Not ours, or already done, or nobody
                               knows. Skipped in every case, which is the whole
                               double-pay defence.

    THE AMOUNT RETURNED IS THE PINNED ONE, not the caller's. A leg first sized
    against one month's net keeps that size when a corrected net re-runs the
    distribution, for the same reason `dividend_run_open` pins its plan: the
    investor is owed what they were promised, and re-pricing mid-run silently
    pays some investors on one basis and some on another.
    """
    amt = int(amount or 0)
    if amt <= 0:
        return 0
    with db_in(conn) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO investor_payout_claims (tag, user_id, amount, state) "
            "VALUES (?,?,?,'claimed')", (str(tag), str(user_id), amt))
        if cur.rowcount == 1:
            return amt
        cur = conn.execute(
            "UPDATE investor_payout_claims SET state='claimed', detail='', "
            " updated_at=datetime('now') WHERE tag=? AND user_id=? AND state='refused'",
            (str(tag), str(user_id)))
        if not cur.rowcount:
            return 0
        row = conn.execute(
            "SELECT amount FROM investor_payout_claims WHERE tag=? AND user_id=?",
            (str(tag), str(user_id))).fetchone()
        return int(row["amount"]) if row else 0


def investor_leg_settle(tag: str, user_id: str, state: str, detail: str = "",
                        *, conn=None) -> bool:
    """Resolve a claimed investor leg to applied / refused / unknown. Pass `conn=`
    to settle it in the same transaction as the money it describes; bare is still
    right for a leg being written down AFTER its transaction rolled back."""
    if state not in ("applied", "refused", "unknown"):
        raise ValueError(f"bad investor leg state {state!r}")
    with db_in(conn) as conn:
        cur = conn.execute(
            "UPDATE investor_payout_claims SET state=?, detail=?, updated_at=datetime('now') "
            "WHERE tag=? AND user_id=? AND state='claimed'",
            (state, str(detail)[:400], str(tag), str(user_id)))
        return cur.rowcount > 0


def investor_leg_state(tag: str, user_id: str):
    """The leg's own state. Used when a `commit()` itself fails: because the marker
    commits WITH the money, the leg IS the receipt — `applied` means the
    transaction landed, and no row at all means it rolled back.

    THE THREE ANSWERS ARE KEPT APART ON PURPOSE. `None` means "no row: the claim
    rolled back with the credit, nothing moved". `"unreadable"` means "the
    database would not answer, so nobody knows". Collapsing those two into one
    `None` — which is what an `except: return None` does — turns a real UNKNOWN
    into "nothing moved", and the caller then pays the investor again on the next
    run. That is the exact shape of every mint this component has had."""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT state FROM investor_payout_claims WHERE tag=? AND user_id=?",
                (str(tag), str(user_id))).fetchone()
        return row["state"] if row else None
    except Exception:
        return "unreadable"


def investor_legs_adopt_stale(tag: str) -> int:
    """Legs still `claimed` when a distribution STARTS were claimed by an attempt
    that died holding them. Their outcome is UNKNOWN — the credit may or may not
    have landed — so they are recorded as such and never automatically re-paid.
    Returns how many. (Same rule and same words as
    `dividend_run_adopt_stale_claims`; re-crediting is the mint both exist to stop.)"""
    with db() as conn:
        cur = conn.execute(
            "UPDATE investor_payout_claims SET state='unknown', updated_at=datetime('now'), "
            " detail=CASE WHEN detail='' THEN 'claimed by an attempt that did not finish; "
            "outcome unknown — check the coin ledger before paying' ELSE detail END "
            "WHERE tag=? AND state='claimed'", (str(tag),))
        return int(cur.rowcount or 0)


def investor_legs(tag: str = None, state: str = None) -> list:
    """The read side: every investor leg, optionally filtered by tag and/or state.
    `state='unknown'` is the operator's "who does nobody know about" list."""
    sql = "SELECT * FROM investor_payout_claims"
    where, args = [], []
    if tag:
        where.append("tag=?"); args.append(str(tag))
    if state:
        where.append("state=?"); args.append(str(state))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY tag, amount DESC, user_id"
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def investor_legs_tally(tag: str) -> dict:
    """{paid, counts:{state:n}, unresolved} for one distribution tag."""
    with db() as conn:
        rows = conn.execute(
            "SELECT state, COUNT(*) n, COALESCE(SUM(amount),0) amt "
            "FROM investor_payout_claims WHERE tag=? GROUP BY state", (str(tag),)).fetchall()
    counts = {r["state"]: int(r["n"]) for r in rows}
    amounts = {r["state"]: int(r["amt"]) for r in rows}
    return {"paid": amounts.get("applied", 0), "counts": counts, "amounts": amounts,
            "unresolved": sum(counts.get(s, 0) for s in ("claimed", "refused", "unknown"))}


def replace_investors(rows: list, total_shares: float = None) -> int:
    """Replace the investor register from a Crimson cap-table export: rows are
    (user_id, name, pref_shares). share_pct is derived from the total so it always sums
    to 100. Existing total_received/joined_at are preserved for returning investors;
    holders no longer on the cap table are removed. Returns how many investors are set.

    total_shares: derive share_pct against THIS total instead of the rows' sum — used when
    liquidated investors are dropped but the company keeps their slice, so the pcts sum
    to <100 and the payout loop simply never pays the liquidated portion out."""
    total = float(total_shares) if total_shares else (sum(float(r[2]) for r in rows) or 1.0)
    with db() as conn:
        keep_ids = [str(r[0]) for r in rows]
        for uid, name, shares in rows:
            conn.execute("""
                INSERT INTO investors (user_id, balance, principal, name, pref_shares, share_pct)
                VALUES (?, 0, 0, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    name=excluded.name, pref_shares=excluded.pref_shares,
                    share_pct=excluded.share_pct, updated_at=datetime('now')
            """, (str(uid), str(name or ""), float(shares), round(100.0 * float(shares) / total, 4)))
        if keep_ids:
            q = ",".join("?" * len(keep_ids))
            conn.execute(f"DELETE FROM investors WHERE user_id NOT IN ({q})", keep_ids)
        return len(rows)



def get_hive_claims() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT * FROM hive_claims").fetchall()
        return {row["location"]: dict(row) for row in rows}


def set_hive_claim(location: str, user_id: str, user_tag: str, claimed_at: str = None):
    with db() as conn:
        conn.execute("""
            INSERT INTO hive_claims (location, user_id, user_tag, claimed_at)
            VALUES (?, ?, ?, COALESCE(?, datetime('now')))
            ON CONFLICT(location) DO UPDATE SET
                user_id=excluded.user_id, user_tag=excluded.user_tag, claimed_at=excluded.claimed_at
        """, (location, str(user_id), user_tag, claimed_at))



def get_hive_batches() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT * FROM hive_batches").fetchall()
        return {row["batch_id"]: json.loads(row["data"]) for row in rows}


def save_hive_batch(batch_id: str, data: dict):
    with db() as conn:
        conn.execute("""
            INSERT INTO hive_batches (batch_id, data)
            VALUES (?, ?)
            ON CONFLICT(batch_id) DO UPDATE SET data=excluded.data
        """, (batch_id, json.dumps(data)))


def get_active_batch_id() -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT batch_id FROM hive_active_batch WHERE id=1").fetchone()
        return row["batch_id"] if row else None


def set_active_batch_id(batch_id: Optional[str]):
    with db() as conn:
        conn.execute("UPDATE hive_active_batch SET batch_id=? WHERE id=1", (batch_id,))



def csn_get_market(market_id: str) -> dict:
    """Return {"months": {month: {label, source, recorded_at, income, spent, net,
    items: {item: {sold_qty, bought_qty, net_coins, times_sold, times_bought,
    income_coins, expense_coins}}}}} for one market."""
    mid = market_id or "main"
    with db() as conn:
        mrows = conn.execute(
            "SELECT * FROM csn_history WHERE market_id=? ORDER BY month", (mid,)).fetchall()
        irows = conn.execute(
            "SELECT * FROM csn_history_items WHERE market_id=?", (mid,)).fetchall()
    items_by_month: dict = {}
    ikeys = set(irows[0].keys()) if irows else set()  # tolerate pre-migration rows
    for r in irows:
        items_by_month.setdefault(r["month"], {})[r["item"]] = {
            "sold_qty":      int(r["sold_qty"] or 0),
            "bought_qty":    int(r["bought_qty"] or 0),
            "net_coins":     float(r["net_coins"] or 0),
            "times_sold":    int(r["times_sold"] or 0) if "times_sold" in ikeys else 0,
            "times_bought":  int(r["times_bought"] or 0) if "times_bought" in ikeys else 0,
            "income_coins":  float(r["income_coins"] or 0) if "income_coins" in ikeys else 0.0,
            "expense_coins": float(r["expense_coins"] or 0) if "expense_coins" in ikeys else 0.0,
        }
    months: dict = {}
    for r in mrows:
        months[r["month"]] = {
            "label":       r["label"] or r["month"],
            "source":      r["source"] or "",
            "recorded_at": r["recorded_at"] or "",
            "income":      float(r["income"] or 0),
            "spent":       float(r["spent"] or 0),
            "net":         float(r["net"] or 0),
            "items":       items_by_month.get(r["month"], {}),
        }
    return {"months": months}


def csn_save_market(market_id: str, data: dict) -> None:
    """Replace all stored months for a market with the given {"months": {...}}
    payload (mirrors the old save-whole-file semantics, atomically)."""
    mid = market_id or "main"
    months = (data or {}).get("months", {}) or {}
    with db() as conn:
        conn.execute("DELETE FROM csn_history WHERE market_id=?", (mid,))
        conn.execute("DELETE FROM csn_history_items WHERE market_id=?", (mid,))
        for mk, md in months.items():
            if not isinstance(md, dict):
                continue
            conn.execute(
                "INSERT INTO csn_history (market_id, month, label, source, recorded_at, income, spent, net)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (mid, mk, md.get("label", ""), md.get("source", ""), md.get("recorded_at", ""),
                 float(md.get("income", 0) or 0), float(md.get("spent", 0) or 0), float(md.get("net", 0) or 0)),
            )
            for item, iv in (md.get("items") or {}).items():
                if not isinstance(iv, dict):
                    continue
                conn.execute(
                    "INSERT INTO csn_history_items (market_id, month, item, sold_qty, bought_qty, net_coins,"
                    " times_sold, times_bought, income_coins, expense_coins)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (mid, mk, item, int(iv.get("sold_qty", 0) or 0),
                     int(iv.get("bought_qty", 0) or 0), float(iv.get("net_coins", 0) or 0),
                     int(iv.get("times_sold", 0) or 0), int(iv.get("times_bought", 0) or 0),
                     float(iv.get("income_coins", 0) or 0), float(iv.get("expense_coins", 0) or 0)),
                )


def _csn_source_profile(items_json: str) -> dict:
    """{item: (sold_qty, bought_qty)} — the shape used to tell two source rows apart."""
    try:
        d = json.loads(items_json or "{}") or {}
    except Exception:
        return {}
    return {k: (float(v.get("sold_qty", 0) or 0), float(v.get("bought_qty", 0) or 0))
            for k, v in d.items() if isinstance(v, dict)}


def _csn_dominates(small_json: str, big_json: str,
                   small_income=None, big_income=None) -> bool:
    """LOOSE test: `big` covers everything `small` reports, and at least as much of it.

    Items a subset, every quantity <=, and (when given) income <=. True of an
    earlier snapshot of the same shop — and ALSO true of a corner shop next to a
    flagship, which is why this is never on its own grounds to delete a row."""
    L, S = _csn_source_profile(small_json), _csn_source_profile(big_json)
    if not L or not S or not set(L) <= set(S):
        return False
    if not all(L[k][0] <= S[k][0] and L[k][1] <= S[k][1] for k in L):
        return False
    if small_income is not None and big_income is not None:
        return float(small_income or 0) <= float(big_income or 0)
    return True


def _csn_supersedes(legacy_json: str, shop_json: str,
                    legacy_income=None, shop_income=None) -> bool:
    """True when `legacy` is an EARLIER SNAPSHOT OF THE SAME SHOP as `shop`.

    STRICT (audit fix, MARKETS_VERIFY_R1 CSN-6). The old test was only
    subset-of-items + every-quantity-<=, and that is the ORDINARY relationship
    between a corner shop and a flagship in one market: a legacy transport-keyed
    row holding a real second shop's 40,000 coins was DELETEd by an unrelated
    shop's routine stamped upload, the month rolled up 260,000 instead of
    300,000, and nothing recorded that it had happened.

    A snapshot of the same shop and a smaller neighbouring shop are only
    distinguishable by the SHAPE of the catalogue, so the test now demands the
    same catalogue: identical item-name sets, every quantity <=, and income <=.
    An earlier snapshot of one shop still satisfies all three (a scan grows in
    quantity within a month); a different shop that merely sells a subset of the
    flagship's lines does not, because its item set is a strict subset.

    The cost, stated rather than hidden: a pre-stamp snapshot taken before the
    shop started selling a NEW line is no longer auto-retired, so it keeps
    contributing to the month until someone removes it. That direction leaves a
    stale figure that an operator can see and delete; the old direction silently
    destroyed a live one. `csn_retire_superseded_sources` logs those cases by
    name (`_csn_dominates` but not `_csn_supersedes`) instead of acting on them,
    and retirement is now reversible either way."""
    L, S = _csn_source_profile(legacy_json), _csn_source_profile(shop_json)
    if not L or not S or set(L) != set(S):
        return False
    if not all(L[k][0] <= S[k][0] and L[k][1] <= S[k][1] for k in L):
        return False
    if legacy_income is not None and shop_income is not None:
        return float(legacy_income or 0) <= float(shop_income or 0)
    return True


def _csn_archive_source(conn, market_id: str, month: str, row, superseded_by: str,
                        reason: str) -> None:
    """Move ONE source row to csn_month_sources_retired. Archive-then-delete inside
    the CALLER's transaction, so the row can never be gone from both tables."""
    conn.execute(
        "INSERT INTO csn_month_sources_retired (market_id, month, source_key, income,"
        " spent, items_json, superseded_by, reason, retired_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (str(market_id), str(month), str(row["source_key"]),
         float(row["income"] or 0), float(row["spent"] or 0), row["items_json"],
         str(superseded_by), str(reason),
         datetime.now(timezone.utc).isoformat()))
    conn.execute("DELETE FROM csn_month_sources "
                 "WHERE market_id=? AND month=? AND source_key=?",
                 (str(market_id), str(month), str(row["source_key"])))


def _csn_is_retirable_key(key: str) -> bool:
    """Only LEGACY TRANSPORT keys are candidates for shop-stamp retirement.

    `shop:` rows are the stamped identities themselves. `export:` rows are the
    per-sale export ledger's own contribution to the month — a different KIND of
    source, not an earlier snapshot of a shop's monthly, and retiring one would
    delete revenue the monthly file never described."""
    k = str(key or "")
    return not (k.startswith("shop:") or k.startswith("export:"))


def csn_retire_superseded_sources(market_id: str, month: str) -> list:
    """Retire legacy TRANSPORT-keyed source rows that a `shop:` row supersedes.

    Month sources used to be keyed by the Discord channel/poster a file arrived
    from. The mod's `# SHOP` stamp changed the key to shop:<ign>, so every shop that
    re-scanned with the new jar started contributing TWICE — once under its old
    numeric key, once under its shop name. Returns the retired keys.

    Retirement MOVES the row to csn_month_sources_retired (it used to DELETE it),
    and the supersession test is now strict — see `_csn_supersedes`. A row that is
    merely DOMINATED by a stamped row is left alone and logged, because that is
    also what a real smaller shop looks like."""
    retired = []
    with db() as conn:
        rows = conn.execute(
            "SELECT source_key, income, spent, items_json FROM csn_month_sources "
            "WHERE market_id=? AND month=?",
            (str(market_id), str(month))).fetchall()
        shops = [r for r in rows if str(r["source_key"]).startswith("shop:")
                 and not str(r["source_key"]).startswith("shop:unstamped")]
        if not shops:
            return retired          # nothing stamped yet — legacy rows are all we have
        for r in rows:
            key = str(r["source_key"])
            if not _csn_is_retirable_key(key):
                continue
            winner = next((s for s in shops if _csn_supersedes(
                r["items_json"], s["items_json"], r["income"], s["income"])), None)
            if winner is not None:
                _csn_archive_source(conn, market_id, month, r,
                                    str(winner["source_key"]), "shop-stamp")
                retired.append(key)
                continue
            near = next((s for s in shops if _csn_dominates(
                r["items_json"], s["items_json"], r["income"], s["income"])), None)
            if near is not None:
                log.warning(
                    "[csn] %s %s: legacy source %s (income %.0f) is covered by "
                    "stamped %s but sells a SUBSET of its lines — keeping it. If it "
                    "is the same shop pre-stamp, remove it by hand; if it is a real "
                    "second shop, this is correct.",
                    market_id, month, key, float(r["income"] or 0),
                    str(near["source_key"]))
    return retired


def _csn_unstamped_key(items: dict) -> str:
    """The bucket an UNSTAMPED monthly file belongs in: `shop:unstamped:<hash>`.

    Files from mod builds older than the `# SHOP` stamp used to share ONE key,
    `shop:unstamped`, and that key REPLACES. Two different shops on old builds
    therefore annihilated each other: 500,000 then 300,000 read as 300,000, and
    because the month moved DOWN no anomaly fired (MARKETS_VERIFY_R1 CSN-5).

    An unstamped file carries no shop identity at all, so the strongest one
    available is the shape of its catalogue: the set of item names it reports.
    Two shops selling different lines now land in different buckets and SUM;
    the same shop rescanning lands in the same bucket and REPLACES. Growth that
    adds a new line changes the hash, which is handled by the rescan merge in
    `csn_set_unstamped_month_source` — never by adding a second row."""
    import hashlib
    names = sorted({str(k).strip().lower() for k in (items or {}) if str(k).strip()})
    if not names:
        return "shop:unstamped"
    return "shop:unstamped:" + hashlib.sha1(
        "\x00".join(names).encode("utf-8")).hexdigest()[:12]


def csn_set_unstamped_month_source(market_id: str, month: str, income: float,
                                   spent: float, items: dict) -> str:
    """Record an UNSTAMPED monthly file's figures and return the source key used.

    Two jobs, in one transaction:

    1. Bucket by catalogue hash so two different unstamped shops cannot collide
       (`_csn_unstamped_key`).
    2. Retire any EARLIER unstamped snapshot this file strictly covers — items a
       superset, every quantity >=, income >= — so a rescan that picked up a new
       line replaces its predecessor instead of adding to it. Without this,
       changing the hash would DOUBLE-BOOK the month, which is the one direction
       that invents coins.

    The retired row is archived, never deleted, so the ambiguous case (a flagship
    that happens to cover a smaller unstamped shop entirely) is recoverable and
    logged rather than silent. That is strictly better than the previous
    behaviour, where ANY second unstamped file annihilated the first whether it
    covered it or not."""
    key = _csn_unstamped_key(items)
    new_json = json.dumps(items or {}, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT source_key, income, spent, items_json FROM csn_month_sources "
            "WHERE market_id=? AND month=? AND source_key LIKE 'shop:unstamped%'",
            (str(market_id), str(month))).fetchall()
        for r in rows:
            if str(r["source_key"]) == key:
                continue                      # same bucket — the UPSERT replaces it
            if _csn_dominates(r["items_json"], new_json, r["income"], income):
                log.info("[csn] %s %s: unstamped rescan %s covers earlier %s "
                         "(income %.0f) — retiring it into the archive",
                         market_id, month, key, str(r["source_key"]),
                         float(r["income"] or 0))
                _csn_archive_source(conn, market_id, month, r, key, "unstamped-rescan")
        conn.execute(
            "INSERT INTO csn_month_sources (market_id, month, source_key, income, spent,"
            " items_json, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(market_id, month, source_key) DO UPDATE SET "
            "income=excluded.income, spent=excluded.spent, items_json=excluded.items_json,"
            " updated_at=excluded.updated_at",
            (str(market_id), str(month), key, float(income or 0), float(spent or 0),
             new_json, now))
    return key


def csn_add_export_source(market_id: str, month: str, income: float, spent: float,
                          items: dict, shop: str = "") -> str:
    """ACCUMULATE one export file's contribution into the market-month rollup.

    Exports never wrote `csn_month_sources` at all. `csn_history` was merged into
    directly, so a later monthly file — which REPLACES the month with the rollup
    across sources — erased every export that came before it: export 1,000 then a
    disjoint monthly 7,000 read as 7,000, not 8,000 (MARKETS_VERIFY_R1 CSN-4).
    The export's coins were real, per-sale, already in `csn_transactions`, and
    they left the month anyway.

    ACCUMULATE, not replace, because an export carries ONE period's partials while
    a monthly re-aggregates the whole month. That is the same semantics
    `_record_to_market_history(merge=True)` already applies to csn_history, so the
    two stores agree. It is idempotent for free: a re-uploaded export's rows are
    already claimed by the `earn` consumer, so it arrives here with income 0."""
    add_json = json.dumps(items or {}, ensure_ascii=False)
    key = "export:" + (str(shop).strip() or "unstamped")
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        row = conn.execute(
            "SELECT income, spent, items_json FROM csn_month_sources "
            "WHERE market_id=? AND month=? AND source_key=?",
            (str(market_id), str(month), key)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO csn_month_sources (market_id, month, source_key, income,"
                " spent, items_json, updated_at) VALUES (?,?,?,?,?,?,?)",
                (str(market_id), str(month), key, float(income or 0),
                 float(spent or 0), add_json, now))
            return key
        try:
            merged = json.loads(row["items_json"] or "{}") or {}
        except Exception:
            merged = {}
        for item, v in (items or {}).items():
            if not isinstance(v, dict):
                continue
            e = merged.setdefault(item, {"sold_qty": 0, "bought_qty": 0, "net_coins": 0.0})
            e["sold_qty"] = int(e.get("sold_qty", 0) or 0) + int(v.get("sold_qty", 0) or 0)
            e["bought_qty"] = int(e.get("bought_qty", 0) or 0) + int(v.get("bought_qty", 0) or 0)
            e["net_coins"] = round(float(e.get("net_coins", 0) or 0)
                                   + float(v.get("net_coins", 0) or 0), 2)
        conn.execute(
            "UPDATE csn_month_sources SET income=?, spent=?, items_json=?, updated_at=? "
            "WHERE market_id=? AND month=? AND source_key=?",
            (round(float(row["income"] or 0) + float(income or 0), 2),
             round(float(row["spent"] or 0) + float(spent or 0), 2),
             json.dumps(merged, ensure_ascii=False), now,
             str(market_id), str(month), key))
    return key


def csn_set_month_source(market_id: str, month: str, source_key: str,
                         income: float, spent: float, items: dict) -> None:
    """Record ONE uploader's contribution to a market-month. Replaces that uploader's
    previous figures for the same month, so re-uploading its file is idempotent and
    never double-counts.

    AUDIT FIX (high, 2026-08-06): source_key used to be pure TRANSPORT identity (the
    Discord poster id, or the filename). The same physical file arriving by a second
    route — a manager re-uploading it by hand, a webhook rotated to another bot user —
    looked like a brand-new shop and its figures were ADDED to the month, multiplying
    the total. Before inserting a new source we now check whether some other source row
    for this market-month holds byte-identical item figures; if so that is the same
    file, and we update THAT row instead of adding a second one."""
    conn_items = json.dumps(items or {}, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        try:
            mine = conn.execute(
                "SELECT 1 FROM csn_month_sources WHERE market_id=? AND month=? AND source_key=?",
                (str(market_id), str(month), str(source_key))).fetchone()
            if not mine and conn_items not in ("{}", "null"):
                # `export:` rows are excluded: an export is a DIFFERENT KIND of
                # source (one period's per-sale partials) from a monthly, so a
                # month with a single sale, where the two happen to carry the same
                # figures, is not one file arriving twice. Treating it as one would
                # silently drop the monthly and leave the month short.
                twin = conn.execute(
                    "SELECT source_key FROM csn_month_sources "
                    "WHERE market_id=? AND month=? AND items_json=? "
                    "AND income=? AND spent=? AND source_key NOT LIKE 'export:%'",
                    (str(market_id), str(month), conn_items,
                     float(income or 0), float(spent or 0))).fetchone()
                if twin:
                    # Same file, different transport. Keep the original row; just
                    # refresh its timestamp so it doesn't look stale.
                    conn.execute(
                        "UPDATE csn_month_sources SET updated_at=? "
                        "WHERE market_id=? AND month=? AND source_key=?",
                        (now, str(market_id), str(month), str(twin[0])))
                    return
        except Exception:
            pass       # never let the dedup probe block the real write
        conn.execute(
            "INSERT INTO csn_month_sources (market_id, month, source_key, income, spent,"
            " items_json, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(market_id, month, source_key) DO UPDATE SET "
            "income=excluded.income, spent=excluded.spent, items_json=excluded.items_json,"
            " updated_at=excluded.updated_at",
            (str(market_id), str(month), str(source_key), float(income or 0),
             float(spent or 0), conn_items, now))
    # A stamped shop row supersedes whatever that same shop was filed under before the
    # `# SHOP` stamp existed. Outside the connection above so the DELETE sees this row.
    # `shop:unstamped*` covers both the legacy single bucket and the per-catalogue
    # hashed buckets: an unstamped file names no shop, so it can never be the stamped
    # identity that retires a legacy row.
    if str(source_key).startswith("shop:") and not str(source_key).startswith("shop:unstamped"):
        try:
            csn_retire_superseded_sources(market_id, month)
        except Exception:
            pass
        # A shop's MONTHLY re-aggregates that shop's whole month, so it supersedes
        # that shop's own export slices for the month. Retiring them is what keeps
        # the two file types from double-counting their overlap; a later export
        # re-opens the row with only the sales this monthly could not have seen.
        try:
            csn_retire_export_source(market_id, month, str(source_key)[len("shop:"):])
        except Exception:
            pass


def csn_retire_export_source(market_id: str, month: str, shop: str) -> bool:
    """Archive `export:<shop>` for a market-month, because `shop:<shop>`'s own
    monthly file now covers it. Returns True if a row was retired."""
    key = "export:" + str(shop or "").strip()
    if key == "export:":
        return False
    with db() as conn:
        row = conn.execute(
            "SELECT source_key, income, spent, items_json FROM csn_month_sources "
            "WHERE market_id=? AND month=? AND source_key=? COLLATE NOCASE",
            (str(market_id), str(month), key)).fetchone()
        if row is None:
            return False
        log.info("[csn] %s %s: monthly from %s supersedes its own export slice "
                 "(income %.0f) — archived, not added",
                 market_id, month, shop, float(row["income"] or 0))
        _csn_archive_source(conn, market_id, month, row, "shop:" + str(shop),
                            "monthly-supersedes-export")
    return True


def csn_month_totals(market_id: str, month: str) -> dict:
    """The market-month rolled up across EVERY uploader: {income, spent, items, sources}.
    This is the real total for a market scanned by several shops."""
    income = spent = 0.0
    items: dict = {}
    n = 0
    with db() as conn:
        rows = conn.execute(
            "SELECT income, spent, items_json FROM csn_month_sources "
            "WHERE market_id=? AND month=?", (str(market_id), str(month))).fetchall()
    for r in rows:
        n += 1
        income += float(r["income"] or 0)
        spent += float(r["spent"] or 0)
        try:
            part = json.loads(r["items_json"] or "{}")
        except Exception:
            part = {}
        for item, v in (part or {}).items():
            if not isinstance(v, dict):
                continue
            e = items.setdefault(item, {"sold_qty": 0, "bought_qty": 0, "net_coins": 0.0})
            e["sold_qty"] += int(v.get("sold_qty", 0) or 0)
            e["bought_qty"] += int(v.get("bought_qty", 0) or 0)
            e["net_coins"] = round(float(e["net_coins"]) + float(v.get("net_coins", 0) or 0), 2)
    return {"income": round(income, 2), "spent": round(spent, 2),
            "items": items, "sources": n}


def csn_retired_sources(market_id: str, month: str = "") -> list:
    """Everything retirement took out of a market's rollup, newest first.

    The undo half of `csn_retire_superseded_sources` / the unstamped rescan merge:
    a month that reads low can be explained without a backup, and a wrongly
    retired shop can be put back with its own figures."""
    sql = ("SELECT id, market_id, month, source_key, income, spent, items_json,"
           " superseded_by, reason, retired_at FROM csn_month_sources_retired "
           "WHERE market_id=?")
    args = [str(market_id)]
    if month:
        sql += " AND month=?"
        args.append(str(month))
    with db() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC", args).fetchall()]


def csn_all_market_ids() -> list:
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT market_id FROM csn_history").fetchall()]



def get_config(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM bot_config WHERE key=?", (str(key),)).fetchone()
        return row["value"] if row and row["value"] is not None else default


# ── Shareholder voting ──────────────────────────────────────────────────────

def create_proposal(market_id: str, question: str, options: list, created_by: str,
                    closes_at: str) -> int:
    import json as _json
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO vote_proposals (market_id, question, options, created_by, closes_at) "
            "VALUES (?,?,?,?,?)",
            (str(market_id), str(question), _json.dumps(list(options)),
             str(created_by), str(closes_at)))
        return int(cur.lastrowid)


def get_proposal(pid: int):
    import json as _json
    with db() as conn:
        row = conn.execute("SELECT * FROM vote_proposals WHERE id=?", (int(pid),)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["options"] = _json.loads(d["options"])
        except Exception:
            d["options"] = []
        return d


def list_proposals(status: str = None) -> list[dict]:
    import json as _json
    q, args = "SELECT * FROM vote_proposals", []
    if status:
        q += " WHERE status=?"; args.append(str(status))
    q += " ORDER BY id DESC"
    with db() as conn:
        out = []
        for row in conn.execute(q, args).fetchall():
            d = dict(row)
            try:
                d["options"] = _json.loads(d["options"])
            except Exception:
                d["options"] = []
            out.append(d)
        return out


def cast_vote(pid: int, user_id: str, choice_idx: int, weight: float, name: str = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO vote_casts (proposal_id, user_id, choice_idx, weight, name) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(proposal_id, user_id) DO UPDATE SET "
            "choice_idx=excluded.choice_idx, weight=excluded.weight, "
            "name=COALESCE(excluded.name, vote_casts.name), cast_at=datetime('now')",
            (int(pid), str(user_id), int(choice_idx), float(weight), name))


def get_votes(pid: int) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM vote_casts WHERE proposal_id=?", (int(pid),)).fetchall()]


def close_proposal(pid: int) -> None:
    with db() as conn:
        conn.execute("UPDATE vote_proposals SET status='closed' WHERE id=?", (int(pid),))


def create_suggestion(user_id: str, name: str, weight: float, text: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO investor_suggestions (user_id, name, weight, text) VALUES (?,?,?,?)",
            (str(user_id), name, float(weight or 0), str(text)))
        return int(cur.lastrowid)


def get_suggestion(sid: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM investor_suggestions WHERE id=?", (int(sid),)).fetchone()
        return dict(row) if row else None


def list_suggestions(status: str = None, limit: int = 30) -> list[dict]:
    q, args = "SELECT * FROM investor_suggestions", []
    if status:
        q += " WHERE status=?"; args.append(str(status))
    q += " ORDER BY id DESC LIMIT ?"; args.append(int(limit))
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def update_suggestion(sid: int, status: str, response: str = None) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE investor_suggestions SET status=?, response=COALESCE(?, response), "
            "updated_at=datetime('now') WHERE id=?",
            (str(status), response, int(sid)))


# ── Listing escrow ──────────────────────────────────────────────────────────

def create_escrow(party: str, kind: str, value: float, note: str = None) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO escrow_deposits (party, kind, value, note) VALUES (?,?,?,?)",
            (str(party), str(kind), float(value), note))
        return int(cur.lastrowid)


def get_escrow(eid: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM escrow_deposits WHERE id=?", (int(eid),)).fetchone()
        return dict(row) if row else None


def list_escrows(status: str = None) -> list[dict]:
    q, args = "SELECT * FROM escrow_deposits", []
    if status:
        q += " WHERE status=?"; args.append(str(status))
    q += " ORDER BY id DESC"
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def update_escrow(eid: int, status: str, note: str = None) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE escrow_deposits SET status=?, note=COALESCE(?, note), "
            "updated_at=datetime('now') WHERE id=?",
            (str(status), note, int(eid)))


# ── Bonds ────────────────────────────────────────────────────────────────────

def create_bond(market_id: str, name: str, face_total: float, unit_price: float,
                coupon_pct: float, term_months: int, matures_at: str) -> int:
    units_total = int(face_total // unit_price)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO bonds (market_id, name, face_total, unit_price, units_total, "
            "coupon_pct, term_months, matures_at) VALUES (?,?,?,?,?,?,?,?)",
            (str(market_id), str(name or ""), float(face_total), float(unit_price),
             units_total, float(coupon_pct), int(term_months), str(matures_at)))
        return int(cur.lastrowid)


def get_bond(bond_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM bonds WHERE id=?", (int(bond_id),)).fetchone()
        return dict(row) if row else None


def list_bonds(market_id: str = None, status: str = None) -> list[dict]:
    q, args = "SELECT * FROM bonds", []
    conds = []
    if market_id:
        conds.append("market_id=?"); args.append(str(market_id))
    if status:
        conds.append("status=?"); args.append(str(status))
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC"
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def update_bond(bond_id: int, *, conn=None, **fields) -> None:
    """Pass `conn=` to update the bond inside the caller's transaction — a bond
    that flips to `active` because a purchase filled the series must flip in the
    same commit as the coins that filled it."""
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db_in(conn) as conn:
        conn.execute(f"UPDATE bonds SET {cols} WHERE id=?",
                     (*fields.values(), int(bond_id)))


def adjust_bond_holding(bond_id: int, user_id: str, d_units: float, d_invested: float,
                        name: str = None, *, conn=None) -> None:
    """Pass `conn=` to record the units in the same transaction as the coins that
    bought them (see `db_in`)."""
    with db_in(conn) as conn:
        conn.execute(
            "INSERT INTO bond_holdings (bond_id, user_id, units, invested, name) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(bond_id, user_id) DO UPDATE SET "
            "units=units+excluded.units, invested=invested+excluded.invested, "
            "name=COALESCE(excluded.name, bond_holdings.name)",
            (int(bond_id), str(user_id), float(d_units), float(d_invested), name))
        conn.execute("UPDATE bonds SET units_sold=units_sold+? WHERE id=?",
                     (float(d_units), int(bond_id)))


def get_bond_holders(bond_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bond_holdings WHERE bond_id=? AND units > 0",
            (int(bond_id),)).fetchall()
        return [dict(r) for r in rows]


def get_user_bonds(user_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT h.*, b.market_id, b.name AS bond_name, b.coupon_pct, b.unit_price, "
            "b.status, b.matures_at FROM bond_holdings h JOIN bonds b ON b.id=h.bond_id "
            "WHERE h.user_id=? AND h.units > 0", (str(user_id),)).fetchall()
        return [dict(r) for r in rows]


def get_config_prefix(prefix: str) -> dict:
    """All bot_config rows whose key starts with prefix → {key: value}.

    Used to enumerate bindings stored as one key per channel, e.g.
    `hive_feed:<channel_id>` → market_id.

    (There were two identical definitions of this in the module; the second
    shadowed this one and carried the better docstring, which is now here. They
    behaved the same, so nothing changed but the reading — unlike
    `list_futures_orders`, whose shadow silently dropped two parameters.)"""
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM bot_config WHERE key LIKE ?",
                            (str(prefix) + "%",)).fetchall()
        return {r["key"]: r["value"] for r in rows}


def set_config(key, value) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO bot_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key), None if value is None else str(value)),
        )


def adjust_config_number(key, delta: float, *, floor: float = 0.0, conn=None) -> float:
    """CLAIM-FIRST on a numeric `bot_config` value: add `delta` in ONE relative
    UPDATE and return the value afterwards, read inside the same transaction.

    Exists because the exchange insurance fund is a coin pot stored as a config
    key, and `get_config` -> add -> `set_config` is the same lost update
    `adjust_treasury` just stopped being. Measured after that fix landed: two
    concurrent buys each skimmed 52 coins out of their treasuries (correctly,
    relatively) and the pot recorded ONE of them — 52 coins destroyed, no error.
    Coins do not stop needing a claim because they are kept in a string column.

    `floor` clamps the stored value (the pot must not go negative); the returned
    number is what the row now holds, not what the caller asked for."""
    with db_in(conn) as conn:
        conn.execute("INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, '0')",
                     (str(key),))
        conn.execute(
            "UPDATE bot_config SET value = CAST(MAX(?, CAST(COALESCE(value,'0') AS REAL) + ?) "
            "AS TEXT) WHERE key=?", (float(floor), float(delta), str(key)))
        row = conn.execute("SELECT value FROM bot_config WHERE key=?", (str(key),)).fetchone()
        try:
            return float(row["value"]) if row else float(floor)
        except (TypeError, ValueError):
            return float(floor)


def delete_config(key) -> None:
    with db() as conn:
        conn.execute("DELETE FROM bot_config WHERE key=?", (str(key),))


def get_all_config() -> dict:
    with db() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM bot_config").fetchall()}


def set_team_member(worker_id: str, manager_id: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO team_members (worker_id, manager_id) VALUES (?, ?) "
            "ON CONFLICT(worker_id) DO UPDATE SET manager_id=excluded.manager_id",
            (str(worker_id), str(manager_id)))


def remove_team_member(worker_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM team_members WHERE worker_id=?", (str(worker_id),))


def get_manager_of(worker_id: str) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT manager_id FROM team_members WHERE worker_id=?",
                           (str(worker_id),)).fetchone()
        return row["manager_id"] if row else None


def get_team(manager_id: str) -> list:
    with db() as conn:
        return [r["worker_id"] for r in conn.execute(
            "SELECT worker_id FROM team_members WHERE manager_id=? ORDER BY added_at",
            (str(manager_id),)).fetchall()]


def get_all_team_managers() -> list:
    """Every manager who has at least one worker on their team (for the dashboard roster)."""
    with db() as conn:
        return [r["manager_id"] for r in conn.execute(
            "SELECT DISTINCT manager_id FROM team_members").fetchall()]


def set_team_settings(manager_id: str, *, webhook_url: str = "__keep__", channel_id: str = "__keep__") -> None:
    """Upsert a team's delivery binding. Pass webhook_url/channel_id to set (or "" to clear);
    omit a field to leave it unchanged."""
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        row = conn.execute("SELECT webhook_url, channel_id FROM team_settings WHERE manager_id=?",
                           (str(manager_id),)).fetchone()
        cur_wh = row["webhook_url"] if row else None
        cur_ch = row["channel_id"] if row else None
        wh = cur_wh if webhook_url == "__keep__" else (webhook_url or None)
        ch = cur_ch if channel_id == "__keep__" else (channel_id or None)
        conn.execute(
            "INSERT INTO team_settings (manager_id, webhook_url, channel_id, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(manager_id) DO UPDATE SET webhook_url=excluded.webhook_url, "
            "channel_id=excluded.channel_id, updated_at=excluded.updated_at",
            (str(manager_id), wh, ch, now))


def get_team_settings(manager_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM team_settings WHERE manager_id=?",
                           (str(manager_id),)).fetchone()
        return dict(row) if row else None


def record_team_perf(manager_id: str, worker_id: str, kind: str,
                     coins: float = 0.0, points: float = 0.0, qty: int = 0, detail: str = "") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO team_perf_log (manager_id, worker_id, kind, coins, points, qty, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(manager_id), str(worker_id), str(kind),
             float(coins or 0), float(points or 0), int(qty or 0), detail or ""))


def reassign_team_perf(manager_id: str, detail: str, splits: list) -> int:
    """Re-attribute one already-logged order to the people who actually did the work.

    A manager claims and fulfils on their team's behalf — that is the intended workflow —
    so every perf row lands with worker_id == manager_id and the team looks idle. This
    replaces that single row with one row per worker, splitting coins/qty by share.

    `splits` is [(worker_id, qty), ...]. Coins are apportioned by qty so the total is
    preserved exactly: the last worker absorbs the rounding remainder, otherwise repeated
    splits would slowly leak coins out of the ledger.
    """
    if not splits:
        return 0
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM team_perf_log WHERE manager_id=? AND detail=? LIMIT 1",
            (str(manager_id), str(detail))).fetchone()
        if row is None:
            return 0
        src = dict(row)
        total_qty = sum(int(q or 0) for _, q in splits) or 1
        coins = float(src.get("coins") or 0)
        points = float(src.get("points") or 0)
        conn.execute("DELETE FROM team_perf_log WHERE id=?", (src["id"],))
        assigned_c = assigned_p = 0.0
        for n, (wid, q) in enumerate(splits):
            q = int(q or 0)
            last = (n == len(splits) - 1)
            c = round(coins - assigned_c, 4) if last else round(coins * q / total_qty, 4)
            pt = round(points - assigned_p, 4) if last else round(points * q / total_qty, 4)
            assigned_c += c
            assigned_p += pt
            conn.execute(
                "INSERT INTO team_perf_log (manager_id, worker_id, kind, coins, points, qty, detail, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (str(manager_id), str(wid), src["kind"], c, pt, q, src["detail"],
                 src.get("created_at")))
        return len(splits)


def team_perf_exists(manager_id: str, detail: str, kind: str = "order") -> bool:
    """True if a perf-ledger row already exists for this manager+detail+kind.
    Used by the backfill to stay idempotent (never double-credit an order)."""
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM team_perf_log WHERE manager_id=? AND detail=? AND kind=? LIMIT 1",
            (str(manager_id), str(detail), str(kind))).fetchone() is not None


def get_team_perf(manager_id: str, since_iso: str = None) -> list:
    with db() as conn:
        if since_iso:
            rows = conn.execute(
                "SELECT * FROM team_perf_log WHERE manager_id=? AND created_at>=? ORDER BY created_at",
                (str(manager_id), since_iso)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM team_perf_log WHERE manager_id=? ORDER BY created_at",
                (str(manager_id),)).fetchall()
        return [dict(r) for r in rows]


def get_all_team_perf(since_iso: str = None) -> list:
    """Every perf row (optionally since a cutoff) across all teams - for leaderboards."""
    with db() as conn:
        if since_iso:
            rows = conn.execute(
                "SELECT * FROM team_perf_log WHERE created_at>=? ORDER BY created_at", (since_iso,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM team_perf_log ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def get_etf_holding(user_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM etf_holdings WHERE user_id=?", (str(user_id),)).fetchone()
        return dict(row) if row else None


def get_etf_units(user_id: str) -> float:
    with db() as conn:
        row = conn.execute("SELECT units FROM etf_holdings WHERE user_id=?", (str(user_id),)).fetchone()
        return float(row["units"]) if row else 0.0


def get_etf_total_units() -> float:
    with db() as conn:
        row = conn.execute("SELECT COALESCE(SUM(units),0) AS u FROM etf_holdings").fetchone()
        return float(row["u"]) if row else 0.0


def get_etf_holders() -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM etf_holdings WHERE units > 0.0000001 ORDER BY units DESC").fetchall()
        return [dict(r) for r in rows]


def adjust_etf_units(user_id: str, delta_units: float, delta_cost: float,
                     *, conn=None) -> float:
    """Apply +/- units & cost to a holder; clamps tiny/negative remainders to 0.
    Returns the new unit total.

    Pass `conn=` so a redemption burns the units in the SAME transaction that
    pays for them — two commits meant a death between them paid the redeemer and
    left the units in their name, which is a mint on the next redemption."""
    now = datetime.now(timezone.utc).isoformat()
    with db_in(conn) as conn:
        conn.execute(
            "INSERT INTO etf_holdings (user_id, units, cost_basis, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET units=units+excluded.units, "
            "cost_basis=cost_basis+excluded.cost_basis, updated_at=excluded.updated_at",
            (str(user_id), float(delta_units), float(delta_cost), now))
        row = conn.execute("SELECT units, cost_basis FROM etf_holdings WHERE user_id=?",
                           (str(user_id),)).fetchone()
        u = float(row["units"]) if row else 0.0
        if u <= 0.0000001:
            # COMPARE-AND-SWAP, not a blind zero, AND THE ROWCOUNT IS THE ANSWER.
            # `SET units=0, cost_basis=0 WHERE user_id=?` stored a value computed
            # from the SELECT above, so a concurrent investment landing between
            # the read and the write had its whole stake wiped — coins taken,
            # units gone, no error. Gated on BOTH figures just read (the clamp
            # zeroes both columns, so both have to still be the ones it decided
            # about), a writer that got in first simply wins.
            #
            # Losing the race does NOT mean "zero". It means the row moved, so
            # the caller is told what it now holds rather than the dust reading
            # this call arrived with — returning 0.0 there would report a live
            # stake as burned.
            cb = float(row["cost_basis"] or 0.0) if row else 0.0
            cur = conn.execute("UPDATE etf_holdings SET units=0, cost_basis=0 "
                               "WHERE user_id=? AND units = ? AND cost_basis = ?",
                               (str(user_id), u, cb))
            if cur.rowcount:
                return 0.0
            fresh = conn.execute("SELECT units FROM etf_holdings WHERE user_id=?",
                                 (str(user_id),)).fetchone()
            return float(fresh["units"]) if fresh else 0.0
        return u


def upsert_market_stock(market_id: str, item: str, owner: str = None, stock: int = 0,
                        buy_price: float = None, sell_price: float = None,
                        capacity: int = None, buy_qty: int = None,
                        sell_qty: int = None, scan_ts: str = None) -> None:
    """Record a live shop-stock snapshot for one item. When `capacity` is given
    (computed as barrels × slots × stack size) it is stored as-is; otherwise
    capacity falls back to the legacy high-water mark (max stock ever seen).

    buy_price/sell_price are stored PER UNIT. buy_qty/sell_qty are the shop's listed
    bulk quantity ("Sell <qty> for <price>") — kept so we can tell a per-unit row from a
    legacy per-bulk one (NULL qty = legacy, not trusted for display)."""
    now = datetime.now(timezone.utc).isoformat()
    mid = market_id or "main"
    with db() as conn:
        row = conn.execute("SELECT capacity FROM market_stock WHERE market_id=? AND item=?",
                           (mid, item)).fetchone()
        cur_cap = int(row["capacity"]) if row else 0
        cap = int(capacity) if capacity is not None else max(cur_cap, int(stock or 0))
        conn.execute(
            "INSERT INTO market_stock (market_id, item, owner, stock, capacity, buy_price, sell_price, buy_qty, sell_qty, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(market_id, item) DO UPDATE SET owner=excluded.owner, stock=excluded.stock, "
            "capacity=excluded.capacity, buy_price=excluded.buy_price, sell_price=excluded.sell_price, "
            "buy_qty=excluded.buy_qty, sell_qty=excluded.sell_qty, updated_at=excluded.updated_at",
            (mid, item, owner, int(stock or 0), cap,
             (float(buy_price) if buy_price is not None else None),
             (float(sell_price) if sell_price is not None else None),
             (int(buy_qty) if buy_qty is not None else None),
             (int(sell_qty) if sell_qty is not None else None), now))
        # Snapshot the reading so history survives the overwrite above. One row per day —
        # a second scan the same day replaces it, so the day reflects the latest count.
        # Keyed on the SCAN's own timestamp when the CSV provides one (timestamp_iso),
        # not the ingest instant — a scan uploaded after midnight lands on the right day.
        day = (str(scan_ts)[:10] if scan_ts and len(str(scan_ts)) >= 10 else now[:10])
        try:
            conn.execute(
                "INSERT INTO market_stock_history (market_id, item, day, stock, capacity, updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(market_id, item, day) DO UPDATE SET stock=excluded.stock, "
                "capacity=excluded.capacity, updated_at=excluded.updated_at",
                (mid, item, day, int(stock or 0), cap, now))
        except Exception as e:
            # Never break a live stock write over history — but a silently-swallowed
            # insert made the trend charts quietly incomplete, so at least say it.
            print(f"[db] market_stock_history insert failed for {mid}/{item}: {e}")


def get_market_stock(market_id: str) -> dict:
    mid = market_id or "main"
    with db() as conn:
        rows = conn.execute("SELECT * FROM market_stock WHERE market_id=? ORDER BY item", (mid,)).fetchall()
        return {r["item"]: dict(r) for r in rows}


def get_all_market_stock() -> list:
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM market_stock ORDER BY market_id, item").fetchall()]


def migrate_market_stock(from_market: str, to_market: str, since_iso: str | None = None) -> int:
    """Move live stock rows from one market to another — used to rescue scans that got
    mis-routed to the default market (e.g. a typo'd Market ID). If since_iso is given,
    only rows updated at/after it move (so you can limit it to the last hour). On a
    (market_id, item) collision the moved source row wins. Returns rows moved."""
    src = from_market or "main"
    dst = to_market or "main"
    if src == dst:
        return 0
    where = "market_id = ?"
    params = [src]
    if since_iso:
        where += " AND updated_at >= ?"
        params.append(since_iso)
    with db() as conn:
        n = conn.execute(f"SELECT COUNT(*) AS c FROM market_stock WHERE {where}", params).fetchone()["c"]
        if not n:
            return 0
        # source wins on PK conflict: drop any clashing dest rows first, then move.
        conn.execute(
            f"DELETE FROM market_stock WHERE market_id = ? AND item IN "
            f"(SELECT item FROM market_stock WHERE {where})",
            [dst] + params)
        conn.execute(f"UPDATE market_stock SET market_id = ? WHERE {where}", [dst] + params)
        return int(n)


def clear_market_stock(market_id: str, since_iso: str | None = None) -> int:
    """Delete live-stock rows for a market (optionally only rows updated at/after
    since_iso). Used to flush stale/mis-routed scans out of a market. Returns the
    number of rows deleted."""
    mid = market_id or "main"
    where = "market_id = ?"
    params = [mid]
    if since_iso:
        where += " AND updated_at >= ?"
        params.append(since_iso)
    with db() as conn:
        n = conn.execute(f"SELECT COUNT(*) AS c FROM market_stock WHERE {where}", params).fetchone()["c"]
        if not n:
            return 0
        conn.execute(f"DELETE FROM market_stock WHERE {where}", params)
        return int(n)


def delete_market_stock_item(market_id: str, item: str) -> bool:
    """Delete ONE item's live-stock row from a market (the dashboard shop list).
    Returns True if a row was removed. Used by the owner 'remove item' flow so a
    deletion actually clears the shop entry, not just the CSN history + catalog."""
    mid = market_id or "main"
    with db() as conn:
        cur = conn.execute("DELETE FROM market_stock WHERE market_id=? AND item=?", (mid, item))
        return cur.rowcount > 0


def set_stock_capacity(market_id: str, item: str, capacity: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    mid = market_id or "main"
    with db() as conn:
        conn.execute(
            "INSERT INTO market_stock (market_id, item, stock, capacity, updated_at) VALUES (?,?,0,?,?) "
            "ON CONFLICT(market_id, item) DO UPDATE SET capacity=excluded.capacity, updated_at=excluded.updated_at",
            (mid, item, int(capacity), now))


def set_stock_alarm(market_id: str, item: str, threshold: float, mode: str = "pct") -> None:
    mid = market_id or "main"
    with db() as conn:
        conn.execute(
            "INSERT INTO stock_alarms (market_id, item, threshold, mode) VALUES (?,?,?,?) "
            "ON CONFLICT(market_id, item) DO UPDATE SET threshold=excluded.threshold, mode=excluded.mode",
            (mid, item, float(threshold), mode if mode in ("pct", "pieces") else "pct"))


def delete_stock_alarm(market_id: str, item: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM stock_alarms WHERE market_id=? AND item=?", (market_id or "main", item))


def get_stock_alarms(market_id: str) -> dict:
    with db() as conn:
        rows = conn.execute("SELECT item, threshold, mode FROM stock_alarms WHERE market_id=?",
                           (market_id or "main",)).fetchall()
        return {r["item"]: {"threshold": float(r["threshold"]), "mode": r["mode"]} for r in rows}


def create_project(title: str, funder_id: str, manager_id: str, budget: int) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (title, funder_id, manager_id, budget, status, created_at, updated_at) "
            "VALUES (?,?,?,?, 'open', ?, ?)",
            (title, str(funder_id), str(manager_id), int(budget), now, now))
        return int(cur.lastrowid)


def get_project(project_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (int(project_id),)).fetchone()
        return dict(row) if row else None


def set_project_status(project_id: int, status: str, proof: str = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        if proof is None:
            conn.execute("UPDATE projects SET status=?, updated_at=? WHERE id=?",
                         (status, now, int(project_id)))
        else:
            conn.execute("UPDATE projects SET status=?, proof=?, updated_at=? WHERE id=?",
                         (status, proof, now, int(project_id)))


def list_projects(status: str = None, manager_id: str = None, funder_id: str = None, limit: int = 50) -> list:
    q = "SELECT * FROM projects WHERE 1=1"
    args = []
    if status:
        q += " AND status=?"; args.append(status)
    if manager_id:
        q += " AND manager_id=?"; args.append(str(manager_id))
    if funder_id:
        q += " AND funder_id=?"; args.append(str(funder_id))
    q += " ORDER BY id DESC LIMIT ?"; args.append(int(limit))
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def add_project_member(project_id: int, worker_id: str, share: float = 1.0) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO project_members (project_id, worker_id, share) VALUES (?,?,?) "
            "ON CONFLICT(project_id, worker_id) DO UPDATE SET share=excluded.share",
            (int(project_id), str(worker_id), float(share)))


def remove_project_member(project_id: int, worker_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM project_members WHERE project_id=? AND worker_id=?",
                     (int(project_id), str(worker_id)))


def get_project_members(project_id: int) -> list:
    with db() as conn:
        rows = conn.execute("SELECT worker_id, share FROM project_members WHERE project_id=?",
                           (int(project_id),)).fetchall()
        return [dict(r) for r in rows]


def record_market_index(total_mcap: float, index_value: float, markets: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO market_index_log (total_mcap, index_value, markets) VALUES (?, ?, ?)",
            (float(total_mcap), float(index_value), int(markets)),
        )


def get_market_index_base() -> Optional[float]:
    with db() as conn:
        row = conn.execute(
            "SELECT total_mcap FROM market_index_log ORDER BY id ASC LIMIT 1").fetchone()
        return float(row["total_mcap"]) if row else None


def get_market_index_history(limit: int = 200) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, total_mcap, index_value, markets FROM market_index_log "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_platform_balance() -> float:
    with db() as conn:
        row = conn.execute("SELECT balance FROM platform_balance WHERE id=1").fetchone()
        return row["balance"] if row else 0.0


def set_platform_balance(balance: float):
    with db() as conn:
        conn.execute("UPDATE platform_balance SET balance=? WHERE id=1", (balance,))


def add_platform_balance_log(month: str, market_id: str, amount: float, note: str = None):
    with db() as conn:
        conn.execute("""
            INSERT INTO platform_balance_log (month, market_id, amount, note)
            VALUES (?,?,?,?)
        """, (month, market_id, amount, note))


def platform_fee_exists(month: str, market_id: str, note: str) -> bool:
    """True if a fee with this exact (month, market, note) is already on the platform log.
    Makes recurring charges idempotent — e.g. re-ingesting a CSN month must not re-charge
    that month's platform fee."""
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM platform_balance_log WHERE month=? AND market_id=? AND note=? LIMIT 1",
            (str(month), str(market_id), str(note))).fetchone() is not None


def get_platform_balance_log(limit: int = 10) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM platform_balance_log ORDER BY logged_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def delete_order(order_id: int):
    """Delete an order and its claims from the database."""
    with db() as conn:
        conn.execute("DELETE FROM order_claims WHERE order_id=?", (order_id,))
        conn.execute("DELETE FROM orders WHERE id=?", (order_id,))


def clear_hive_batches():
    """Delete all hive batches and reset the active batch."""
    with db() as conn:
        conn.execute("DELETE FROM hive_batches")
        conn.execute("UPDATE hive_active_batch SET batch_id=NULL WHERE id=1")



def get_loyalty(user_id: str) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM loyalty WHERE user_id=?", (str(user_id),)).fetchone()
        if row:
            return dict(row)
        return {"user_id": str(user_id), "points": 0.0, "total_earned": 0.0, "last_activity": None}


def add_loyalty_points(user_id: str, points: float, *, update_activity: bool = True,
                       conn=None) -> float:
    """Add points to a user. Returns new point total.
    total_earned only ever GROWS: negative deltas (redemption deductions) reduce the balance
    but must not shrink the all-time-earned stat shown in /loyalty stats.

    Pass `conn=` to run inside the caller's transaction — a bare `+= points` is
    not idempotent, so a retried reversal has to commit the points move and its
    idempotency row together (see `db_in`)."""
    now = datetime.now(timezone.utc).isoformat()
    with db_in(conn) as conn:
        conn.execute("""
            INSERT INTO loyalty (user_id, points, total_earned, last_activity, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                points       = points + excluded.points,
                total_earned = total_earned + CASE WHEN excluded.points > 0 THEN excluded.points ELSE 0 END,
                last_activity = CASE WHEN ? THEN excluded.last_activity ELSE last_activity END,
                updated_at   = excluded.updated_at
        """, (str(user_id), points, max(0.0, points), now if update_activity else None, now, int(update_activity)))
        row = conn.execute("SELECT points FROM loyalty WHERE user_id=?", (str(user_id),)).fetchone()
        return row["points"] if row else points


def set_loyalty_points(user_id: str, points: float) -> float:
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("""
            INSERT INTO loyalty (user_id, points, total_earned, last_activity, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                points=excluded.points, updated_at=excluded.updated_at
        """, (str(user_id), max(0.0, points), max(0.0, points), now, now))
        return max(0.0, points)


def get_loyalty_leaderboard(limit: int = 20) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, points, total_earned, last_activity FROM loyalty ORDER BY points DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_loyalty() -> list:
    with db() as conn:
        rows = conn.execute("SELECT * FROM loyalty").fetchall()
        return [dict(r) for r in rows]


def update_loyalty_points_bulk(updates: list[tuple]):
    """updates = [(new_points, user_id), ...]"""
    with db() as conn:
        conn.executemany("UPDATE loyalty SET points=?, updated_at=datetime('now') WHERE user_id=?", updates)


# ── Per-market loyalty ledger (Stage 4) ───────────────────────────────────────────────
def get_market_loyalty(user_id: str, market_id: str) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM market_loyalty_ledger WHERE user_id=? AND market_id=?",
            (str(user_id), str(market_id))).fetchone()
        if row:
            return dict(row)
        return {"user_id": str(user_id), "market_id": str(market_id),
                "points": 0.0, "total_earned": 0.0, "last_activity": None}


def add_market_loyalty_points(user_id: str, market_id: str, points: float,
                              *, update_activity: bool = True, conn=None) -> float:
    """Add points to a user's ledger for ONE market — each market owner's own reward
    currency, independent of every other market and of the shared V Tech pool (the
    `loyalty` table). Returns the new point total for that (user, market) pair.

    Pass `conn=` to run inside the caller's transaction (see `db_in`)."""
    now = datetime.now(timezone.utc).isoformat()
    with db_in(conn) as conn:
        conn.execute("""
            INSERT INTO market_loyalty_ledger (user_id, market_id, points, total_earned, last_activity, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, market_id) DO UPDATE SET
                points        = points + excluded.points,
                total_earned  = total_earned + CASE WHEN excluded.points > 0 THEN excluded.points ELSE 0 END,
                last_activity = CASE WHEN ? THEN excluded.last_activity ELSE last_activity END,
                updated_at    = excluded.updated_at
        """, (str(user_id), str(market_id), points, max(0.0, points),
              now if update_activity else None, now, int(update_activity)))
        row = conn.execute(
            "SELECT points FROM market_loyalty_ledger WHERE user_id=? AND market_id=?",
            (str(user_id), str(market_id))).fetchone()
        return row["points"] if row else points


def set_market_loyalty_points(user_id: str, market_id: str, points: float) -> float:
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("""
            INSERT INTO market_loyalty_ledger (user_id, market_id, points, total_earned, last_activity, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, market_id) DO UPDATE SET
                points=excluded.points, updated_at=excluded.updated_at
        """, (str(user_id), str(market_id), max(0.0, points), max(0.0, points), now, now))
        return max(0.0, points)


def get_market_loyalty_leaderboard(market_id: str, limit: int = 20) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, points, total_earned, last_activity FROM market_loyalty_ledger "
            "WHERE market_id=? ORDER BY points DESC LIMIT ?", (str(market_id), int(limit))).fetchall()
        return [dict(r) for r in rows]


def get_all_market_loyalty_for_user(user_id: str) -> list:
    """Every market ledger this user has a nonzero balance or history in, richest first —
    powers the per-market breakdown on /loyalty stats."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM market_loyalty_ledger WHERE user_id=? AND (points > 0 OR total_earned > 0) "
            "ORDER BY points DESC", (str(user_id),)).fetchall()
        return [dict(r) for r in rows]



def get_ign(user_id: str) -> Optional[str]:
    """The user's PRIMARY in-game name (earliest registered) — what displays everywhere a
    single IGN is shown. Use get_igns() for the full main+alts list."""
    with db() as conn:
        row = conn.execute(
            "SELECT ign FROM ign_registry WHERE user_id=? ORDER BY registered_at ASC, ign ASC LIMIT 1",
            (str(user_id),)).fetchone()
        return row["ign"] if row else None


def get_igns(user_id: str) -> list:
    """Every in-game name this user has registered (main + alts), primary/earliest first."""
    with db() as conn:
        rows = conn.execute(
            "SELECT ign FROM ign_registry WHERE user_id=? ORDER BY registered_at ASC, ign ASC",
            (str(user_id),)).fetchall()
        return [r["ign"] for r in rows]


def count_igns(user_id: str) -> int:
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM ign_registry WHERE user_id=?",
                           (str(user_id),)).fetchone()
        return int(row["c"] if row else 0)


def get_user_id_by_ign(ign: str) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT user_id FROM ign_registry WHERE ign=? COLLATE NOCASE", (str(ign).strip(),)).fetchone()
        return row["user_id"] if row else None


def add_ign(user_id: str, ign: str) -> str:
    """Register one in-game name for a user (main or alt). Returns:
      'added'  — newly linked to this user
      'exists' — this user already had that IGN (idempotent no-op)
      'taken'  — the IGN belongs to a DIFFERENT user (caller should refuse)
    Does NOT enforce the per-user count cap — that's a command-layer policy check."""
    ign = str(ign).strip()
    owner = get_user_id_by_ign(ign)
    if owner is not None:
        return "exists" if str(owner) == str(user_id) else "taken"
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO ign_registry (ign, user_id, registered_at) VALUES (?, ?, ?)",
            (ign, str(user_id), now))
    return "added"


def set_ign(user_id: str, ign: str) -> str:
    """Back-compat shim: registration paths call this to link an IGN. Now ADDS (alts are
    allowed) rather than replacing the user's single IGN. Returns add_ign()'s status."""
    return add_ign(user_id, ign)


def remove_ign(user_id: str, ign: str) -> bool:
    """Remove ONE specific IGN from a user (e.g. a mistyped alt). Returns True if a row was
    deleted. The user keeps their other IGNs; primary falls through to the next-earliest."""
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM ign_registry WHERE user_id=? AND ign=? COLLATE NOCASE",
            (str(user_id), str(ign).strip()))
        return cur.rowcount > 0


def delete_ign(user_id: str):
    """Remove ALL of a user's IGNs (full unlink)."""
    with db() as conn:
        conn.execute("DELETE FROM ign_registry WHERE user_id=?", (str(user_id),))



def set_ign_pending(user_id: str, dm_channel_id: str, role_id: str, guild_id: str, deadline: str):
    with db() as conn:
        conn.execute("""
            INSERT INTO ign_pending (user_id, dm_channel_id, role_id, guild_id, deadline)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                dm_channel_id=excluded.dm_channel_id,
                role_id=excluded.role_id,
                guild_id=excluded.guild_id,
                deadline=excluded.deadline
        """, (str(user_id), str(dm_channel_id), str(role_id), str(guild_id), deadline))


def get_ign_pending(user_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM ign_pending WHERE user_id=?", (str(user_id),)).fetchone()
        return dict(row) if row else None


def get_all_ign_pending() -> list:
    with db() as conn:
        rows = conn.execute("SELECT * FROM ign_pending").fetchall()
        return [dict(r) for r in rows]


def delete_ign_pending(user_id: str):
    with db() as conn:
        conn.execute("DELETE FROM ign_pending WHERE user_id=?", (str(user_id),))



def save_note(text: str, author_id: str, author_name: str) -> int:
    """Save a note; returns the new note ID."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO notes (author_id, author_name, text) VALUES (?, ?, ?)",
            (str(author_id), author_name, text),
        )
        return cur.lastrowid


def list_notes(author_id: str | None = None, limit: int = 10) -> list[dict]:
    """Return recent notes, optionally filtered by author."""
    with db() as conn:
        if author_id:
            rows = conn.execute(
                "SELECT id, author_name, text, created_at FROM notes "
                "WHERE author_id=? ORDER BY created_at DESC LIMIT ?",
                (str(author_id), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, author_name, text, created_at FROM notes "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]



# ── Enchant-area roster ──────────────────────────────────────────────────────
# Which employees (by IGN) operate which enchant-table AREA (a /la land area).
# For now managers supply the IGNs manually; onboarding will auto-bind later.
def _ensure_enchant_area_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS enchant_area_members ("
        "  area      TEXT NOT NULL,"
        "  ign       TEXT NOT NULL,"
        "  user_id   TEXT,"                # resolved from ign_registry if known, else NULL
        "  added_by  TEXT,"
        "  added_at  TEXT NOT NULL DEFAULT (datetime('now')),"
        "  PRIMARY KEY (area, ign)"
        ")")


def enchant_area_add(area: str, ign: str, added_by: str = "") -> str:
    """Bind one IGN to an enchant area. Returns 'added' or 'exists'."""
    area = str(area).strip()
    ign = str(ign).strip()
    if not area or not ign:
        return "skip"
    uid = get_user_id_by_ign(ign)
    with db() as conn:
        _ensure_enchant_area_table(conn)
        row = conn.execute(
            "SELECT 1 FROM enchant_area_members WHERE area=? COLLATE NOCASE AND ign=? COLLATE NOCASE",
            (area, ign)).fetchone()
        if row:
            # refresh the resolved user_id in case they registered since
            conn.execute(
                "UPDATE enchant_area_members SET user_id=COALESCE(?, user_id) "
                "WHERE area=? COLLATE NOCASE AND ign=? COLLATE NOCASE", (uid, area, ign))
            return "exists"
        conn.execute(
            "INSERT INTO enchant_area_members (area, ign, user_id, added_by) VALUES (?,?,?,?)",
            (area, ign, uid, str(added_by) or None))
        return "added"


def enchant_area_list(area: str | None = None) -> list[dict]:
    """All roster rows, or just one area's. Ordered by area then ign."""
    with db() as conn:
        _ensure_enchant_area_table(conn)
        if area:
            rows = conn.execute(
                "SELECT area, ign, user_id, added_by, added_at FROM enchant_area_members "
                "WHERE area=? COLLATE NOCASE ORDER BY ign COLLATE NOCASE", (str(area).strip(),)).fetchall()
        else:
            rows = conn.execute(
                "SELECT area, ign, user_id, added_by, added_at FROM enchant_area_members "
                "ORDER BY area COLLATE NOCASE, ign COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def enchant_area_remove(area: str, ign: str) -> bool:
    with db() as conn:
        _ensure_enchant_area_table(conn)
        cur = conn.execute(
            "DELETE FROM enchant_area_members WHERE area=? COLLATE NOCASE AND ign=? COLLATE NOCASE",
            (str(area).strip(), str(ign).strip()))
        return cur.rowcount > 0


def enchant_area_clear(area: str) -> int:
    with db() as conn:
        _ensure_enchant_area_table(conn)
        cur = conn.execute(
            "DELETE FROM enchant_area_members WHERE area=? COLLATE NOCASE", (str(area).strip(),))
        return cur.rowcount


def save_web_order(discord_username: str, discord_id: str, items: list, notes: str = "") -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO web_orders (discord_username, discord_id, items_json, notes)
               VALUES (?, ?, ?, ?)""",
            (discord_username, discord_id or "", json.dumps(items), notes or "")
        )
        return cur.lastrowid


def get_web_order(order_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM web_orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None


def update_web_order_status(order_id: int, status: str, reviewed_by: str = "", notify_msg_id: str = "") -> None:
    with db() as conn:
        conn.execute(
            """UPDATE web_orders SET status=?, reviewed_by=?, reviewed_at=datetime('now'), notify_msg_id=?
               WHERE id=?""",
            (status, reviewed_by, notify_msg_id, order_id)
        )


def list_web_orders(status: str = None, limit: int = 50) -> list:
    with db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM web_orders WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM web_orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]



def save_futures_order(user_id: str, username: str, item: str, quantity: int,
                        enchants: str = "", notes: str = "") -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO futures_orders (user_id, username, item, quantity, enchants, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(user_id), username, item, int(quantity), enchants or "", notes or "")
        )
        return cur.lastrowid


def get_futures_order(order_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM futures_orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None


def get_futures_order_by_msg(notify_msg_id) -> Optional[dict]:
    """Recover a futures order from the message its buttons live on.

    Persistent views are registered as FuturesOrderView(0), so after a restart the view
    has NO order id — every button reported "Order not found" on any message posted
    before that restart. This is the same recovery the bulk view already used.
    """
    if not notify_msg_id:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM futures_orders WHERE notify_msg_id=? LIMIT 1",
            (str(notify_msg_id),)).fetchone()
        return dict(row) if row else None


def get_web_order_by_msg(notify_msg_id) -> Optional[dict]:
    """Same recovery for web orders — WebOrderView(0) has the identical flaw."""
    if not notify_msg_id:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM web_orders WHERE notify_msg_id=? LIMIT 1",
            (str(notify_msg_id),)).fetchone()
        return dict(row) if row else None


def update_futures_order_status(order_id: int, status: str, reviewed_by: str = "", notify_msg_id: str = "") -> None:
    with db() as conn:
        conn.execute(
            """UPDATE futures_orders SET status=?, reviewed_by=?, reviewed_at=datetime('now'), notify_msg_id=?
               WHERE id=?""",
            (status, reviewed_by, notify_msg_id, order_id)
        )


def list_futures_orders(status: str = None, user_id: str = None, limit: int = 50) -> list:
    with db() as conn:
        if status and user_id:
            rows = conn.execute(
                "SELECT * FROM futures_orders WHERE status=? AND user_id=? ORDER BY created_at DESC LIMIT ?",
                (status, str(user_id), limit)
            ).fetchall()
        elif status:
            rows = conn.execute(
                "SELECT * FROM futures_orders WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        elif user_id:
            rows = conn.execute(
                "SELECT * FROM futures_orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (str(user_id), limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM futures_orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# ── Bulk / consignment futures ────────────────────────────────────────────────────────
def create_futures_bulk(customer_id: str, customer_name: str, market_id: str,
                        created_by: str, notes: str = "") -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO futures_bulk (customer_id, customer_name, market_id, created_by, notes) "
            "VALUES (?,?,?,?,?)",
            (str(customer_id), str(customer_name or ""), str(market_id or ""),
             str(created_by or ""), str(notes or "")))
        return int(cur.lastrowid)


def add_futures_bulk_line(bulk_id: int, item: str, qty: int, unit: str = "pieces",
                          enchants: str = "", raw_line: str = "", item_key: str = None,
                          worker_cost: float = None, full_price: float = None) -> int:
    """item_key: when the line was picked from the catalog (web builder), link it immediately
    so consignment pricing/CSN matching doesn't need a manual /futures price item match."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO futures_bulk_lines (bulk_id, item, qty, unit, enchants, raw_line, "
            "item_key, worker_cost, full_price) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(bulk_id), str(item), int(qty), str(unit or "pieces"),
             str(enchants or ""), str(raw_line or ""), (str(item_key) if item_key else None),
             (float(worker_cost) if worker_cost is not None else None),
             (float(full_price) if full_price is not None else None)))
        return int(cur.lastrowid)


def get_futures_bulk_lines(bulk_id: int) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM futures_bulk_lines WHERE bulk_id=? ORDER BY id ASC", (int(bulk_id),)).fetchall()
        return [dict(r) for r in rows]


def get_futures_bulk(bulk_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM futures_bulk WHERE id=?", (int(bulk_id),)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["lines"] = get_futures_bulk_lines(bulk_id)
        return d


def get_futures_bulk_by_msg(notify_msg_id) -> Optional[dict]:
    """Recover a bulk order from the review message its buttons live on — lets the persistent
    view work after a restart without carrying the id on the view instance."""
    if not notify_msg_id:
        return None
    with db() as conn:
        row = conn.execute("SELECT id FROM futures_bulk WHERE notify_msg_id=?",
                           (str(notify_msg_id),)).fetchone()
    return get_futures_bulk(int(row["id"])) if row else None


def list_futures_bulk(status: str = None, customer_id: str = None, limit: int = 25) -> list:
    with db() as conn:
        if status and customer_id:
            rows = conn.execute("SELECT * FROM futures_bulk WHERE status=? AND customer_id=? "
                                "ORDER BY created_at DESC LIMIT ?", (status, str(customer_id), limit)).fetchall()
        elif status:
            rows = conn.execute("SELECT * FROM futures_bulk WHERE status=? ORDER BY created_at DESC LIMIT ?",
                                (status, limit)).fetchall()
        elif customer_id:
            rows = conn.execute("SELECT * FROM futures_bulk WHERE customer_id=? ORDER BY created_at DESC LIMIT ?",
                                (str(customer_id), limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM futures_bulk ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def update_futures_bulk_status(bulk_id: int, status: str, reviewed_by: str = None,
                               notify_msg_id: str = None) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE futures_bulk SET status=?, "
            "reviewed_by=COALESCE(?, reviewed_by), "
            "reviewed_at=CASE WHEN ? IN ('fulfilled','declined','cancelled') THEN datetime('now') ELSE reviewed_at END, "
            "notify_msg_id=COALESCE(?, notify_msg_id) WHERE id=?",
            (status, reviewed_by, status, notify_msg_id, int(bulk_id)))


def set_futures_order_bulk_line(order_id: int, line_id: int) -> None:
    """Tie a futures order to the bulk line it came from."""
    with db() as conn:
        conn.execute("UPDATE futures_orders SET bulk_line_id=? WHERE id=?",
                     (int(line_id), int(order_id)))


def get_futures_bulk_line(line_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM futures_bulk_lines WHERE id=?",
                           (int(line_id),)).fetchone()
        return dict(row) if row else None


def get_futures_bulk_lines_all() -> list:
    """Every bulk line with its bulk's status — for backfills that must know whether the
    deal is live before touching its pricing."""
    with db() as conn:
        rows = conn.execute(
            "SELECT l.*, b.status AS bulk_status, b.due_at AS bulk_due_at, "
            "       b.customer_name AS customer_name "
            "FROM futures_bulk_lines l JOIN futures_bulk b ON b.id = l.bulk_id "
            "ORDER BY l.bulk_id, l.id").fetchall()
        return [dict(r) for r in rows]


def claim_futures_line_charge(line_id: int) -> bool:
    """Atomically mark a line as charged. True means THIS caller won the race and must do
    the debit; False means it was already charged. Claim-first, exactly like the hive
    payout path — never debit and then mark, or a crash between the two double-charges."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE futures_bulk_lines SET charged_at=datetime('now') "
            "WHERE id=? AND (charged_at IS NULL OR charged_at='')", (int(line_id),))
        return cur.rowcount > 0


def unclaim_futures_line_charge(line_id: int) -> None:
    """Release the claim if the debit itself failed, so it can be retried."""
    with db() as conn:
        conn.execute("UPDATE futures_bulk_lines SET charged_at=NULL WHERE id=?", (int(line_id),))


def get_futures_line_by_work_order(work_order_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT l.*, b.customer_id, b.customer_name FROM futures_bulk_lines l "
            "JOIN futures_bulk b ON b.id=l.bulk_id WHERE l.work_order_id=?",
            (int(work_order_id),)).fetchone()
        return dict(row) if row else None


def set_futures_bulk_line_pricing(line_id: int, worker_cost: float, full_price: float) -> None:
    with db() as conn:
        conn.execute("UPDATE futures_bulk_lines SET worker_cost=?, full_price=? WHERE id=?",
                     (float(worker_cost), float(full_price), int(line_id)))


def set_futures_bulk_due(bulk_id: int, due_at_iso: str) -> bool:
    """Start the consignment clock. No-op if already set, so re-approving another line
    of the same bulk can't quietly extend the deadline."""
    with db() as conn:
        row = conn.execute("SELECT due_at FROM futures_bulk WHERE id=?", (int(bulk_id),)).fetchone()
        if row is None or (row["due_at"] or "").strip():
            return False
        conn.execute("UPDATE futures_bulk SET due_at=? WHERE id=?", (str(due_at_iso), int(bulk_id)))
        return True


def set_futures_bulk_line_baseline(line_id: int, sold_baseline: int) -> None:
    """Freeze the customer's CURRENT sold count for this item at approval time, so the
    consignment invoice only charges margin on units resold AFTER the deal — not on
    everything they had ever sold before it."""
    with db() as conn:
        conn.execute("UPDATE futures_bulk_lines SET sold_baseline=? WHERE id=?",
                     (int(sold_baseline), int(line_id)))


def set_futures_bulk_line_order(line_id: int, work_order_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE futures_bulk_lines SET work_order_id=? WHERE id=?",
                     (int(work_order_id), int(line_id)))


def set_futures_bulk_line_prices(line_id: int, worker_cost: float = None,
                                 full_price: float = None) -> None:
    """Stage B: set a line's per-unit break-even (worker_cost) and full price. Either may be
    omitted to leave it unchanged."""
    with db() as conn:
        conn.execute(
            "UPDATE futures_bulk_lines SET "
            "worker_cost=COALESCE(?, worker_cost), full_price=COALESCE(?, full_price) WHERE id=?",
            (worker_cost, full_price, int(line_id)))


def price_futures_bulk_line(line_id: int, item_key: str, worker_cost: float,
                            full_price: float, sold_baseline: int) -> None:
    """Stage B: lock a line's consignment pricing — link it to a catalog item (for CSN resale
    matching), snapshot the per-unit break-even + full price, and record the customer's current
    CSN cumulative sold as the baseline (only resales AFTER this count toward the bill)."""
    with db() as conn:
        conn.execute(
            "UPDATE futures_bulk_lines SET item_key=?, worker_cost=?, full_price=?, "
            "sold_baseline=? WHERE id=?",
            (str(item_key or ""), float(worker_cost or 0), float(full_price or 0),
             int(sold_baseline or 0), int(line_id)))


def set_futures_bulk_line_sold(line_id: int, sold_override, sold_qty: int = None) -> None:
    """Set a line's manual resold override (pass None to clear it and fall back to CSN auto),
    and optionally cache the last-computed CSN resold count."""
    with db() as conn:
        conn.execute(
            "UPDATE futures_bulk_lines SET sold_override=?, "
            "sold_qty=COALESCE(?, sold_qty) WHERE id=?",
            (None if sold_override is None else int(sold_override),
             None if sold_qty is None else int(sold_qty), int(line_id)))


def record_futures_bulk_payment(bulk_id: int, amount: float) -> float:
    """Add a customer payment against a bulk deal's owed margin. Returns the new paid total."""
    with db() as conn:
        conn.execute("UPDATE futures_bulk SET paid = paid + ? WHERE id=?",
                     (float(amount), int(bulk_id)))
        row = conn.execute("SELECT paid FROM futures_bulk WHERE id=?", (int(bulk_id),)).fetchone()
        return float(row["paid"]) if row else 0.0


def set_item_worker_cost(name: str, worker_cost) -> None:
    """Set an item's break-even cost (used as the default when pricing a consignment line).
    Pass None to clear it."""
    with db() as conn:
        conn.execute("UPDATE items SET worker_cost=? WHERE name=?",
                     (None if worker_cost is None else float(worker_cost), str(name)))


# ── Hive engine ──────────────────────────────────────────────────────────────

def add_hive_harvest(market_id: str, ign: str, user_id, item: str, qty: int,
                     unit_value: float, msg_id: str, line_no: int, sale_ts: str = None,
                     wage_value: float = None, sale_sig: str = None):
    """Record one parsed harvest line. Returns the new row id if it was NEW, else None.
    The id lets auto-payout settle exactly the rows it just created.

    TWO DEDUP REGIMES, chosen by whether the caller can identify the sale exactly:

    `sale_sig` GIVEN (CSN export path) — the caller has a csn_sig content signature,
    which is exactly reproducible from the sale's own content. Uniqueness against other
    SIGNED rows is that signature (uq_hive_sig) and nothing else: the ±120s heuristic
    is not consulted between two signed rows, because there it is not a safety net, it
    is a bug — two genuinely separate harvests of the same item and quantity by the
    same player inside two minutes are indistinguishable to it, so the second was
    dropped and that harvester was never paid for it. Exactly-once is already
    guaranteed upstream: the caller claims the row's `hive_state` in csn_ingest before
    calling (claim-first), so this is reached at most once per (market, signature) even
    across bot instances.

    A signed row IS still checked against UNSIGNED rows by the window, and that half is
    load-bearing twice over:
      - MIGRATION. Every wage already in this table predates signatures and has
        sale_sig NULL. The first export uploaded after the upgrade re-presents the
        whole append-only period file, so without this check every one of those sales
        would arrive as a brand-new signed row and be PAID A SECOND TIME. Caught by
        test_csn_migration.py check 6.
      - THE OTHER FEED. The csn-hive webhook posts the same physical sales as unsigned
        rows. A signed export row for a sale the feed already paid must lose.

    `sale_sig` OMITTED (csn-hive webhook feed) — unchanged behaviour, and the window
    still sees every row, signed or not, so a feed line for a sale the export already
    paid is refused. A feed line carries no seller, verb or amount and cannot be
    signed, so it keeps the msg+line key, the market+ign+item+qty+sale_ts identity
    index, and the window that covers this path's real drift."""
    with db() as conn:
        # NEAR-DUPLICATE GUARD. uq_hive_sale keys on the EXACT sale_ts, but the mod
        # reconstructs that timestamp from "Xh Ym ago" — minute precision — so the SAME
        # sale gets a slightly different absolute time on every run (observed: 14:18:21
        # vs 14:18:47 for one sale, and three rows spanning 48s for another). Each new
        # timestamp slipped past the unique index as a "new" sale and was PAID AGAIN.
        # Measured damage before this guard: 76 duplicate rows, 63,211 coins overpaid.
        # Two ingest paths make it worse — the csn-hive webhook lines and the export CSV
        # both describe the same sales with independently-drifting timestamps.
        # So: same market+ign+item+qty within ±120s is the same sale, full stop.
        #
        # AUDIT FIX (high, 2026-08-06): the guard below no longer filters on market_id.
        # A sale is a physical event — the same honey leaving the same barrel — and the
        # market id is just how the exporter happened to be configured at the time. When
        # the mod's market id changes between two runs (or an export CSV is bound to one
        # market while the hive webhook feed is bound to another), the identical sale
        # landed once per market and EACH market settled it independently. That is live
        # in the database right now: JesseNapoleon's four Honey Block sales exist under
        # both 'greyhames' and 'vtech' with byte-identical timestamps. Harvester+item+qty
        # within ±120s is one sale no matter which market claims it; the first market to
        # record it owns it.
        if sale_ts:
            mine = _csn_ts_seconds(sale_ts)
            if mine is not None:
                # A SIGNED row compares itself only against UNSIGNED rows. Two signed
                # rows are told apart exactly by uq_hive_sig, so letting this fuzzy
                # window arbitrate between them is what silently swallowed a real
                # second harvest. Against unsigned rows the window is all there is.
                sql = ("SELECT sale_ts FROM hive_harvests WHERE ign=? COLLATE NOCASE "
                       "AND item=? AND qty=? AND sale_ts IS NOT NULL")
                if sale_sig:
                    sql += " AND sale_sig IS NULL"
                for row in conn.execute(
                        sql, (str(ign), str(item), int(qty))).fetchall():
                    other = _csn_ts_seconds(row[0])
                    if other is not None and abs(other - mine) <= 120:
                        return None            # already ingested — never pay twice
        cur = conn.execute(
            "INSERT OR IGNORE INTO hive_harvests "
            "(market_id, ign, user_id, item, qty, unit_value, wage_value, msg_id, line_no, "
            " sale_ts, sale_sig) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(market_id), str(ign), (str(user_id) if user_id else None), str(item),
             int(qty), float(unit_value or 0), float(wage_value or 0), str(msg_id), int(line_no),
             (str(sale_ts) if sale_ts else None),
             (str(sale_sig) if sale_sig else None)))
        return int(cur.lastrowid) if cur.rowcount > 0 else None


def get_hive_harvests_by_ids(ids: list) -> list:
    if not ids:
        return []
    with db() as conn:
        q = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT * FROM hive_harvests WHERE id IN ({q}) ORDER BY id",
                            [int(i) for i in ids]).fetchall()
        return [dict(r) for r in rows]


def ign_unpaid_value(ign: str) -> float:
    """Coins of UNPAID, UNLINKED harvest value waiting on an IGN. Anti-squatting:
    an IGN with money attached can't be self-claimed — a manager must link it."""
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(qty*unit_value),0) AS v FROM hive_harvests "
            "WHERE lower(ign)=lower(?) AND paid=0 AND user_id IS NULL", (str(ign),)).fetchone()
        return float(row["v"] or 0)


def get_hive_msg_lines(msg_id: str) -> list:
    """(ign, qty, item) for every row already ingested from one message —
    content-multiset dedup for cumulative feeds that prepend or rewrite."""
    with db() as conn:
        rows = conn.execute(
            "SELECT ign, qty, item FROM hive_harvests WHERE msg_id=?",
            (str(msg_id),)).fetchall()
        return [(str(r["ign"]), int(r["qty"]), str(r["item"])) for r in rows]


def hive_lines_for_msg(msg_id: str) -> int:
    """How many lines of a message are already ingested (edit-reingest support)."""
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM hive_harvests WHERE msg_id=?",
                           (str(msg_id),)).fetchone()
        return int(row["c"] if row else 0)


def get_unpaid_hive_harvests(market_id: str) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM hive_harvests WHERE market_id=? AND paid=0 ORDER BY id",
            (str(market_id),)).fetchall()
        return [dict(r) for r in rows]


def hive_markets_with_unpaid() -> list:
    """Every market that currently has unpaid harvest rows. The 6-hourly autopay sweep
    used to discover markets ONLY from bound `hive_feed:` channels, so a market whose
    harvests arrive through the CSN export path (no webhook feed channel bound) was
    never swept — its stragglers sat unpaid forever."""
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT market_id FROM hive_harvests WHERE paid=0").fetchall()]


def mark_hive_harvests_paid(ids: list) -> int:
    if not ids:
        return 0
    with db() as conn:
        q = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE hive_harvests SET paid=1, paid_at=datetime('now') "
            f"WHERE id IN ({q}) AND paid=0", [int(i) for i in ids])
        return cur.rowcount


def claim_hive_harvests(ids: list) -> list:
    """Claim rows for payment and return the ids THIS call actually flipped.

    mark_hive_harvests_paid only reports HOW MANY it claimed, which is not enough when
    two settle runs overlap: on a partial claim the caller released its whole id list
    and un-paid rows the other run had already moved coins for, so the next sweep paid
    them a second time. Callers must release exactly what this returned, never the
    list they asked for."""
    if not ids:
        return []
    want = [int(i) for i in ids]
    q = ",".join("?" * len(want))
    with db() as conn:
        try:
            rows = conn.execute(
                f"UPDATE hive_harvests SET paid=1, paid_at=datetime('now') "
                f"WHERE id IN ({q}) AND paid=0 RETURNING id", want).fetchall()
            return [int(r[0]) for r in rows]
        except Exception:
            # RETURNING needs SQLite 3.35+. The read and the write share one connection
            # and one transaction, so no other writer can slip between them.
            claimable = [int(r[0]) for r in conn.execute(
                f"SELECT id FROM hive_harvests WHERE id IN ({q}) AND paid=0",
                want).fetchall()]
            if claimable:
                q2 = ",".join("?" * len(claimable))
                conn.execute(
                    f"UPDATE hive_harvests SET paid=1, paid_at=datetime('now') "
                    f"WHERE id IN ({q2}) AND paid=0", claimable)
            return claimable


def unmark_hive_harvests_paid(ids: list) -> int:
    """Release a claim taken by claim_hive_harvests — used when the payment that
    followed the claim failed, so the rows go back to payable. Pass ONLY the ids
    claim_hive_harvests returned; releasing rows another run claimed double-pays them."""
    if not ids:
        return 0
    with db() as conn:
        q = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE hive_harvests SET paid=0, paid_at=NULL "
            f"WHERE id IN ({q}) AND paid=1", [int(i) for i in ids])
        return cur.rowcount


def set_hive_harvest_user(ign: str, user_id: str) -> int:
    """Attach a user to any UNPAID rows for an IGN that was unregistered at ingest time —
    run when someone registers late so their back-harvests become payable."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE hive_harvests SET user_id=? WHERE ign=? COLLATE NOCASE "
            "AND user_id IS NULL AND paid=0", (str(user_id), str(ign).strip()))
        return cur.rowcount


def add_hive_booking(market_id: str, month: str, value: float,
                     harvester_pay: float, owner_pay: float) -> dict:
    """Accumulate one payout run's economics into the market's monthly hive ledger.
    net (V Tech's gain) = value − harvester pay − owner cut."""
    net = float(value) - float(harvester_pay) - float(owner_pay)
    with db() as conn:
        conn.execute("""
            INSERT INTO hive_ledger (market_id, month, value, harvester_pay, owner_pay, net)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(market_id, month) DO UPDATE SET
                value=value+excluded.value, harvester_pay=harvester_pay+excluded.harvester_pay,
                owner_pay=owner_pay+excluded.owner_pay, net=net+excluded.net,
                updated_at=datetime('now')
        """, (str(market_id), str(month), float(value), float(harvester_pay),
              float(owner_pay), net))
        row = conn.execute("SELECT * FROM hive_ledger WHERE market_id=? AND month=?",
                           (str(market_id), str(month))).fetchone()
        return dict(row) if row else {}


def get_hive_ledger_months(market_id: str) -> dict:
    """{month: {value, harvester_pay, owner_pay, net}} — full hive economics for the
    website ledger (CSN shows 0 for hive shops, so the money view merges this in)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT month, value, harvester_pay, owner_pay, net FROM hive_ledger "
            "WHERE market_id=?", (str(market_id),)).fetchall()
        return {r["month"]: {"value": float(r["value"] or 0),
                             "harvester_pay": float(r["harvester_pay"] or 0),
                             "owner_pay": float(r["owner_pay"] or 0),
                             "net": float(r["net"] or 0)} for r in rows}


def get_hive_months(market_id: str) -> dict:
    """{month: net} — the hive engine's monthly V Tech gain, added on top of CSN months
    by the stock roll-up."""
    with db() as conn:
        rows = conn.execute("SELECT month, net FROM hive_ledger WHERE market_id=?",
                            (str(market_id),)).fetchall()
        return {r["month"]: float(r["net"] or 0) for r in rows}


def get_hive_harvest_summary(market_id: str) -> dict:
    """Per-month harvest rollup for one hive site.

    {month: {"qty", "value", "paid_value", "by_ign": {ign: {"qty","value"}},
             "by_item": {item: {"qty","value"}}}}

    Month comes from the in-game sale timestamp when the CSN mod supplied one,
    else from when the line was recorded.
    """
    out = {}
    with db() as conn:
        rows = conn.execute("""
            SELECT COALESCE(substr(sale_ts,1,7), substr(recorded_at,1,7)) AS month,
                   ign, item,
                   SUM(qty)                                      AS qty,
                   SUM(qty * unit_value)                          AS value,
                   SUM(CASE WHEN paid=1 THEN qty*unit_value ELSE 0 END) AS paid_value,
                   -- wage basis: 0 means the column predates the split, so read it as
                   -- unit_value and the row behaves exactly as it did before.
                   SUM(qty * COALESCE(NULLIF(wage_value,0), unit_value))  AS wage_base,
                   SUM(CASE WHEN paid=1
                            THEN qty*COALESCE(NULLIF(wage_value,0), unit_value)
                            ELSE 0 END)                           AS paid_wage_base
            FROM hive_harvests
            WHERE market_id=?
            GROUP BY month, ign, item
        """, (str(market_id),)).fetchall()
    for r in rows:
        mk = r["month"] or "unknown"
        m = out.setdefault(mk, {"qty": 0, "value": 0.0, "paid_value": 0.0,
                                "wage_base": 0.0, "paid_wage_base": 0.0,
                                "by_ign": {}, "by_item": {}})
        q, v = int(r["qty"] or 0), float(r["value"] or 0)
        wb, pwb = float(r["wage_base"] or 0), float(r["paid_wage_base"] or 0)
        m["qty"] += q
        m["value"] += v
        m["paid_value"] += float(r["paid_value"] or 0)
        m["wage_base"] += wb
        m["paid_wage_base"] += pwb
        g = m["by_ign"].setdefault(r["ign"], {"qty": 0, "value": 0.0,
                                              "wage_base": 0.0, "paid_wage_base": 0.0})
        g["qty"] += q; g["value"] += v; g["wage_base"] += wb; g["paid_wage_base"] += pwb
        i = m["by_item"].setdefault(r["item"], {"qty": 0, "value": 0.0,
                                                "wage_base": 0.0, "paid_wage_base": 0.0})
        i["qty"] += q; i["value"] += v; i["wage_base"] += wb; i["paid_wage_base"] += pwb
    return out


def _csn_ts_seconds(ts: str):
    """ISO timestamp → epoch seconds, or None. Tolerates the mod's trailing 'Z'."""
    try:
        from datetime import datetime as _dt
        s = str(ts).strip().replace("Z", "+00:00")
        return _dt.fromisoformat(s).timestamp()
    except Exception:
        return None


CSN_CONSUMERS = ("txn", "earn", "hive", "feed")


def _csn_ingest_row_payload(market_id: str, r: dict) -> dict:
    """Canonicalise one parsed CSV row into what `csn_ingest` stores.

    Raises csn_sig.SigError when the row cannot be signed. That is deliberately
    not caught here: a row we cannot sign is a row we cannot dedup, and quietly
    dropping it is exactly how a market's revenue goes missing without a trace.
    The caller reports it and counts it."""
    import csn_sig

    occ = r.get("occ")
    legacy = 0 if occ else 1
    if not occ:
        # Pre-v3 mod (no occ column). csn_sig.assign_occurrences has already
        # numbered the batch; a single row arriving alone is simply occurrence 1.
        occ = 1
    sale_date = r.get("sale_date") or (str(r.get("sale_ts") or "")[:10])
    coins_src = r.get("coins_str", r.get("coins"))
    payload = {
        "link_id":      str(market_id),
        "seller":       csn_sig.norm_name(r.get("seller")),
        "actor":        csn_sig.norm_name(r.get("actor")),
        "verb":         str(r.get("verb") or "").strip().lower(),
        "item_raw":     csn_sig.norm_item(r.get("item_raw") or r.get("item") or ""),
        "item_display": str(r.get("item") or "").strip(),
        # Cleaned (colour codes, whitespace) but NOT case-folded — this is the name a
        # human reads. norm_item does exactly that shaping and nothing else.
        "actor_display":  csn_sig.norm_item(r.get("actor")),
        "seller_display": csn_sig.norm_item(r.get("seller")),
        "qty":          int(r.get("qty") or 0),
        "coins_centi":  csn_sig.coins_to_centi(coins_src),
        "sale_date":    sale_date,
        "occ":          int(occ),
        "sale_ts":      str(r.get("sale_ts") or "") or None,
        "source_key":   str(r.get("source_key") or "") or None,
        "source_file":  str(r.get("source_file") or "") or None,
        "legacy":       legacy,
    }
    # Signed through `csn_sig.sig_for_row` — the SAME row-dict marshalling the
    # parity tests exercise — rather than by unpacking the eight fields here.
    # Both spellings produce the identical digest today (the normalisers are
    # idempotent), but they were two copies of one rule: the tests proved
    # `sig_for_row` agreed with the mod while production hand-rolled its own
    # argument order, so a change to either could drift without a failing test.
    # `sig_for_row` had no production caller at all before this.
    payload["sig"] = csn_sig.sig_for_row({
        "seller":    payload["seller"],
        "actor":     payload["actor"],
        "verb":      payload["verb"],
        "qty":       payload["qty"],
        "item_raw":  payload["item_raw"],
        "coins_str": _centi_to_decimal_str(payload["coins_centi"]),
        "sale_date": payload["sale_date"],
        "occ":       payload["occ"],
    })
    return payload


def _centi_to_decimal_str(centi: int) -> str:
    """Integer centi-coins back to the canonical 2dp decimal string the signature
    hashes. Round-trips exactly: `coins_to_centi(_centi_to_decimal_str(n)) == n`."""
    sign = "-" if centi < 0 else ""
    whole, frac = divmod(abs(int(centi)), 100)
    return f"{sign}{whole}.{frac:02d}"


def csn_ingest_record(market_id: str, rows: list) -> dict:
    """Land parsed CSN rows in the ONE durable store. Insert-and-catch IS the dedup.

    Returns {"new": n, "dup": n, "bad": n, "ids": [...], "errors": [...]} where
    `ids` are the csn_ingest row ids for EVERY row that is now stored — new or
    already-present — so a caller can drive the consumers over a re-uploaded file
    and still have each consumer act exactly once (its own flag decides, not this
    function's notion of "new").

    Midnight edge: a row whose reconstructed time-of-day falls within
    csn_sig.DRIFT_SECONDS of midnight could have been filed under either of two
    dates on a previous walk, so before inserting we probe the adjacent date for
    the same content. That is a bounded, exact, two-key lookup over a 60-second
    band — not the ±90s fuzzy window it replaces, which fired on every row and
    could merge two genuinely distinct sales."""
    import csn_sig

    out = {"new": 0, "dup": 0, "bad": 0, "ids": [], "errors": []}
    if not rows:
        return out
    now = _utcnow_iso()
    with db() as conn:
        for r in rows:
            try:
                p = _csn_ingest_row_payload(market_id, r)
            except Exception as exc:                       # unsignable row
                out["bad"] += 1
                out["errors"].append(f"{r.get('item') or '?'}: {exc}")
                continue

            # Exact hit first — the overwhelmingly common case.
            hit = conn.execute(
                "SELECT id FROM csn_ingest WHERE link_id=? AND sig=?",
                (p["link_id"], p["sig"])).fetchone()

            # Midnight boundary: the same sale may already be stored under the
            # previous date. Probe by CONTENT (not by time proximity) on exactly
            # the dates csn_sig says are reachable for this row.
            if hit is None:
                alts = csn_sig.boundary_dates(p["sale_date"], p.get("sale_ts"))
                for alt_date in alts[1:]:
                    alt_sig = csn_sig.sale_sig(
                        p["seller"], p["actor"], p["verb"], p["qty"], p["item_raw"],
                        _centi_to_decimal_str(p["coins_centi"]), alt_date, p["occ"])
                    hit = conn.execute(
                        "SELECT id FROM csn_ingest WHERE link_id=? AND sig=?",
                        (p["link_id"], alt_sig)).fetchone()
                    if hit is not None:
                        break

            if hit is not None:
                out["dup"] += 1
                out["ids"].append(int(hit[0]))
                conn.execute(
                    "UPDATE csn_ingest SET seen_count = seen_count + 1, last_seen_at=? "
                    "WHERE id=?", (now, int(hit[0])))
                continue

            try:
                cur = conn.execute("""
                    INSERT INTO csn_ingest
                        (link_id, sig, seller, actor, verb, item_raw, item_display,
                         actor_display, seller_display,
                         qty, coins_centi, sale_date, occ, sale_ts, source_key,
                         source_file, legacy, first_seen_at, last_seen_at)
                    VALUES (:link_id, :sig, :seller, :actor, :verb, :item_raw,
                            :item_display, :actor_display, :seller_display,
                            :qty, :coins_centi, :sale_date, :occ,
                            :sale_ts, :source_key, :source_file, :legacy, :now, :now)
                """, dict(p, now=now))
                out["new"] += 1
                out["ids"].append(int(cur.lastrowid))
            except sqlite3.IntegrityError:
                # Lost a race with another instance on the SAME gateway event.
                # The unique index is the arbiter; this is a duplicate, not an error.
                dup = conn.execute(
                    "SELECT id FROM csn_ingest WHERE link_id=? AND sig=?",
                    (p["link_id"], p["sig"])).fetchone()
                out["dup"] += 1
                if dup:
                    out["ids"].append(int(dup[0]))
                    conn.execute(
                        "UPDATE csn_ingest SET seen_count = seen_count + 1, "
                        "last_seen_at=? WHERE id=?", (now, int(dup[0])))
    return out


def csn_claim(consumer: str, row_ids: list) -> list:
    """Claim rows for one consumer, claim-first. Returns the ids actually WON.

    One atomic `UPDATE ... WHERE <consumer>_state='pending'` per row; the caller
    acts only on the ids this returns. Two bot instances, a manual re-drop racing
    the loop, or an impatient double-click cannot both win the same row, so the
    effect runs once even though both callers saw it as pending.

    The claim is written BEFORE the effect (rule 1) and per row (rule 2): a crash
    mid-effect leaves that row 'claimed', which `csn_stuck_claims` surfaces, and
    every other row's progress stands. Nothing replays."""
    if consumer not in CSN_CONSUMERS:
        raise ValueError(f"unknown CSN consumer {consumer!r}")
    if not row_ids:
        return []
    col = f"{consumer}_state"
    won = []
    now = _utcnow_iso()
    with db() as conn:
        for rid in row_ids:
            cur = conn.execute(
                f"UPDATE csn_ingest SET {col}='claimed', {consumer}_at=? "
                f"WHERE id=? AND {col}='pending'", (now, int(rid)))
            if cur.rowcount:
                won.append(int(rid))
    return won


def csn_settle(consumer: str, row_ids: list, state: str = "done") -> int:
    """Mark claimed rows finished ('done') or not-applicable ('skip'). Per row.

    Only moves rows this consumer is holding, so a settle can never overwrite a
    claim another instance won in between."""
    if consumer not in CSN_CONSUMERS:
        raise ValueError(f"unknown CSN consumer {consumer!r}")
    if state not in ("done", "skip", "pending"):
        raise ValueError(f"bad settle state {state!r}")
    if not row_ids:
        return 0
    col = f"{consumer}_state"
    now = _utcnow_iso()
    n = 0
    with db() as conn:
        for rid in row_ids:
            cur = conn.execute(
                f"UPDATE csn_ingest SET {col}=?, {consumer}_at=? "
                f"WHERE id=? AND {col}='claimed'", (state, now, int(rid)))
            n += cur.rowcount
    return n


def csn_ingest_rows(row_ids: list) -> list:
    """Fetch stored rows by id, oldest sale first — the order a consumer books in."""
    if not row_ids:
        return []
    ids = [int(i) for i in row_ids]
    marks = ",".join("?" * len(ids))
    with db() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM csn_ingest WHERE id IN ({marks}) "
            f"ORDER BY sale_date, COALESCE(sale_ts,''), occ, id", ids).fetchall()]


def csn_pending(consumer: str, market_id: str = None, limit: int = 500) -> list:
    """Rows this consumer has not processed. Drives catch-up without a second store."""
    if consumer not in CSN_CONSUMERS:
        raise ValueError(f"unknown CSN consumer {consumer!r}")
    col = f"{consumer}_state"
    sql = f"SELECT * FROM csn_ingest WHERE {col}='pending'"
    args = []
    if market_id:
        sql += " AND link_id=?"
        args.append(str(market_id))
    sql += " ORDER BY sale_date, COALESCE(sale_ts,''), occ, id LIMIT ?"
    args.append(int(limit))
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def csn_stuck_claims(older_than_minutes: int = 30) -> list:
    """Rows left 'claimed' — a consumer died mid-effect. The operator surface.

    These are NOT auto-released: releasing one would replay an effect that may
    have already landed, which is the double-book this whole design exists to
    prevent. They are reported so a human decides, with the row's real figures in
    front of them."""
    cutoff = _utcnow_iso(minutes_ago=int(older_than_minutes))
    out = []
    with db() as conn:
        for consumer in CSN_CONSUMERS:
            col = f"{consumer}_state"
            rows = conn.execute(
                f"SELECT * FROM csn_ingest WHERE {col}='claimed' "
                f"AND COALESCE({consumer}_at,'') < ? ORDER BY id", (cutoff,)).fetchall()
            for r in rows:
                d = dict(r)
                d["stuck_consumer"] = consumer
                out.append(d)
    return out


def _utcnow_iso(minutes_ago: int = 0) -> str:
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    return (_dt.now(_tz.utc) - _td(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def add_csn_transactions_detailed(market_id: str, rows: list) -> tuple:
    """Bulk-insert per-transaction sales. Returns (new_count, new_rows).

    `rows` are dicts with actor/seller/verb/item/qty/coins/sale_ts and (mod v3+)
    item_raw/sale_date/occ.

    DEDUP LIVES IN `csn_ingest` NOW. This function records every row into that one
    durable store (insert-and-catch on UNIQUE(link_id, sig)), claims the `txn`
    consumer for the rows it wins, and writes csn_transactions for exactly those.
    It is not a second, parallel implementation: `csn_transactions` remains the
    per-sale reporting table, but it is no longer the thing that decides what is a
    duplicate.

    What this replaces, and why:
      - `sale_uid` was minted in the mod from a MINUTE BUCKET of a timestamp
        reconstructed as `now - "Xm ago"`. That is walk-dependent: the same sale
        re-read lands in a different bucket about one time in three and hashed to
        a different uid, so a re-scan re-booked it as fresh revenue.
      - the ±90s near-duplicate window bolted on to cover that fired on EVERY row
        and could not tell "one sale read twice" from "two identical sales 40
        seconds apart" — so it fixed double-counting by introducing
        under-counting.
    `csn_sig.sale_sig` is exactly reproducible from the row's own content, so
    neither error is reachable. See csn_sig for what is in the signature and what
    is deliberately not.

    Rows from a PRE-v3 mod (no `occ`) are still accepted: `csn_sig.assign_occurrences`
    numbers the batch bot-side and the row is flagged `legacy=1`. Such a row can
    only be matched against other legacy readings of the same file, which is why
    the mod ships `occ` — but a mixed fleet keeps working during a rollout.

    Returning the rows that were actually NEW lets the caller book earnings from
    exactly what entered the ledger — a re-uploaded file books nothing twice."""
    if not rows:
        return 0, []

    import csn_sig
    csn_sig.assign_occurrences(rows)
    landed = csn_ingest_record(market_id, rows)
    if landed["bad"]:
        log.warning("[csn ingest] %s: %d row(s) could not be signed and were NOT "
                    "stored: %s", market_id, landed["bad"], "; ".join(landed["errors"][:5]))

    claimed = csn_claim("txn", landed["ids"])
    if not claimed:
        return 0, []

    new = 0
    new_rows = []
    now = _utcnow_iso()
    for src in csn_ingest_rows(claimed):
        ts = src.get("sale_ts") or f"{src['sale_date']}T00:00:00+00:00"
        coins = src["coins_centi"] / 100.0   # csn_transactions column is REAL (legacy)
        try:
            # ONE transaction per row: the ledger insert and this row's progress
            # marker commit together, so a crash can never leave the effect
            # applied with the row still pending (replay) or the row settled with
            # no effect (silent loss). Per row, never after the loop (rule 2).
            with db() as conn:
                cur = conn.execute("""
                    INSERT OR IGNORE INTO csn_transactions
                        (market_id, actor, seller, verb, item, qty, coins, sale_ts,
                         sale_day, sale_uid)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (str(market_id),
                      # DISPLAY names, not the canonical ones. This table feeds the
                      # per-customer ledger and the website; a customer called
                      # JesseNapoleon must not appear there as jessenapoleon just
                      # because the signature happens to fold case.
                      src["actor_display"] or src["actor"],
                      src["seller_display"] or src["seller"], src["verb"],
                      src["item_display"] or src["item_raw"], int(src["qty"]),
                      coins, ts, src["sale_date"], src["sig"]))
                # An IGNOREd insert means some index in csn_transactions overruled
                # the signature. That used to be invisible — the row vanished and the
                # coins with it. Settle it as 'skip', not 'done', and say so: 'done'
                # would claim an effect that never happened.
                ignored = (cur.rowcount == 0)
                conn.execute(
                    "UPDATE csn_ingest SET txn_state=?, txn_at=? "
                    "WHERE id=? AND txn_state='claimed'",
                    ("skip" if ignored else "done", now, int(src["id"])))
            if ignored:
                log.error("[csn txn] %s: ledger REFUSED sig %s (%s %dx %s for %s) — "
                          "an index in csn_transactions rejected a row csn_ingest "
                          "accepted. Not counted as booked.",
                          market_id, src["sig"][:12], src["actor"], int(src["qty"]),
                          src["item_display"] or src["item_raw"],
                          _centi_to_decimal_str(src["coins_centi"]))
                continue
        except Exception as exc:
            log.warning("[csn txn] insert failed for sig %s: %s", src["sig"][:12], exc)
            continue
        new += 1
        new_rows.append({
                # Display names again — callers of this put them straight into embeds.
                "actor": src["actor_display"] or src["actor"],
                "seller": src["seller_display"] or src["seller"], "verb": src["verb"],
                "item": src["item_display"] or src["item_raw"], "qty": int(src["qty"]),
                "coins": coins, "coins_centi": int(src["coins_centi"]),
                "sale_ts": ts, "sale_uid": src["sig"], "sig": src["sig"],
                "ingest_id": int(src["id"]), "item_raw": src["item_raw"],
                "sale_date": src["sale_date"], "occ": int(src["occ"]),
            })
    return new, new_rows


def _add_csn_transactions_legacy(market_id: str, rows: list) -> tuple:
    """The pre-`csn_ingest` implementation, kept ONLY as executable documentation of
    what was wrong with it. Not called. See add_csn_transactions_detailed."""
    if not rows:
        return 0, []
    new = 0
    new_rows = []
    with db() as conn:
        for r in rows:
            ts = str(r.get("sale_ts") or "").strip()
            if not ts:
                continue                       # a sale with no time is useless here
            uid = (str(r.get("sale_uid") or "").strip() or None)
            actor = str(r.get("actor") or "?")
            seller = str(r.get("seller") or "")
            verb = str(r.get("verb") or "").lower()
            item = str(r.get("item") or "")
            qty = int(r.get("qty") or 0)
            coins = float(r.get("coins") or 0)
            try:
                if uid:
                    dup = conn.execute(
                        "SELECT 1 FROM csn_transactions WHERE market_id=? AND sale_uid=?",
                        (str(market_id), uid)).fetchone()
                    if dup:
                        continue
                # AUDIT FIX (high, 2026-08-06): the ±90s window now runs for EVERY row,
                # not only rows without a sale_uid. A uid MISS used to fall straight
                # through to INSERT, which defeated the whole point of the window: the
                # mod derives sale_ts from a minute-granularity "Xm ago" string, so a
                # re-read of the same sale can land in the next minute bucket and hash
                # to a DIFFERENT uid (measured: ~1 re-read in 3). Two alts scanning the
                # same market did it constantly, and 182 of the live rows have no uid at
                # all so a re-export with a uid could never match them. Verified: the
                # same production sale replayed 60s later inserted a second time and its
                # coins were booked as fresh earnings.
                mine = _csn_ts_seconds(ts)
                if mine is not None:
                    cands = conn.execute(
                        "SELECT id, sale_ts, sale_uid FROM csn_transactions "
                        "WHERE market_id=? AND actor=? "
                        "AND COALESCE(seller,'')=? AND verb=? AND item=? AND qty=? AND coins=?",
                        (str(market_id), actor, seller, verb, item, qty, coins)).fetchall()
                    near_id = None
                    near_uid = None
                    for c in cands:
                        other = _csn_ts_seconds(c[1])
                        if other is not None and abs(other - mine) <= 90:
                            near_id, near_uid = c[0], c[2]
                            break
                    if near_id is not None:
                        # Backfill the uid onto the legacy row so the cheap fast path
                        # catches this sale next time instead of re-scanning.
                        if uid and not near_uid:
                            try:
                                conn.execute(
                                    "UPDATE csn_transactions SET sale_uid=? WHERE id=?",
                                    (uid, int(near_id)))
                            except Exception:
                                pass       # a uid collision here just means no fast path
                        continue
                cur = conn.execute("""
                    INSERT OR IGNORE INTO csn_transactions
                        (market_id, actor, seller, verb, item, qty, coins, sale_ts, sale_day, sale_uid)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (str(market_id), actor, seller, verb, item, qty, coins, ts, ts[:10], uid))
                if cur.rowcount:
                    new += 1
                    new_rows.append(dict(r))
            except Exception:
                continue
    return new, new_rows


def add_csn_transactions(market_id: str, rows: list) -> int:
    """Back-compat wrapper: insert and return only the NEW count."""
    n, _ = add_csn_transactions_detailed(market_id, rows)
    return n


def get_csn_daily_sales(market_id: str, days: int = 30) -> list:
    """[{day, income, spent, net, units, txns, customers}] newest first."""
    with db() as conn:
        rows = conn.execute("""
            SELECT sale_day AS day,
                   SUM(CASE WHEN verb='bought' THEN coins ELSE 0 END)      AS income,
                   SUM(CASE WHEN verb='sold'   THEN ABS(coins) ELSE 0 END) AS spent,
                   SUM(CASE WHEN verb='bought' THEN qty ELSE 0 END)        AS units,
                   COUNT(*)                                                AS txns,
                   COUNT(DISTINCT actor)                                   AS customers
            FROM csn_transactions
            WHERE market_id=?
            GROUP BY sale_day
            ORDER BY sale_day DESC
            LIMIT ?
        """, (str(market_id), int(days))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["net"] = float(d["income"] or 0) - float(d["spent"] or 0)
        out.append(d)
    return out


def get_csn_day_detail(market_id: str, day: str) -> list:
    """What sold on ONE day: [{item, units, coins, txns}] best-selling first."""
    with db() as conn:
        rows = conn.execute("""
            SELECT item,
                   SUM(CASE WHEN verb='bought' THEN qty ELSE 0 END)   AS units,
                   SUM(CASE WHEN verb='bought' THEN coins ELSE 0 END) AS coins,
                   COUNT(*)                                           AS txns
            FROM csn_transactions
            WHERE market_id=? AND sale_day=?
            GROUP BY item
            ORDER BY coins DESC
        """, (str(market_id), str(day))).fetchall()
    return [dict(r) for r in rows]


def get_csn_top_customers(market_id: str, days: int = 30, limit: int = 15) -> list:
    """Who actually spends money here: [{actor, spent, units, txns, last_seen}]."""
    with db() as conn:
        rows = conn.execute("""
            SELECT actor,
                   SUM(coins) AS spent,
                   SUM(qty)   AS units,
                   COUNT(*)   AS txns,
                   MAX(sale_ts) AS last_seen
            FROM csn_transactions
            WHERE market_id=? AND verb='bought'
              AND sale_day >= date('now', ?)
            GROUP BY actor
            ORDER BY spent DESC
            LIMIT ?
        """, (str(market_id), f"-{int(days)} days", int(limit))).fetchall()
    return [dict(r) for r in rows]


def get_stock_history(market_id: str, item: str = None, days: int = 60) -> list:
    """Raw stock readings: [{item, day, stock, capacity}] oldest first."""
    q = ("SELECT item, day, stock, capacity FROM market_stock_history "
         "WHERE market_id=? AND day >= date('now', ?)")
    args = [str(market_id), f"-{int(days)} days"]
    if item:
        q += " AND item=?"
        args.append(str(item))
    q += " ORDER BY day"
    with db() as conn:
        return [dict(r) for r in conn.execute(q, args).fetchall()]


def get_stock_depletion(market_id: str, days: int = 30) -> list:
    """Per item: how fast stock is falling and when it runs out.

    [{item, stock, capacity, per_day, days_left, readings, first_day, last_day}]
    sorted by urgency (soonest to empty first). `per_day` is average units lost per
    day across the window — negative means it's being restocked faster than it sells,
    in which case days_left is None. Needs >= 2 readings on different days.
    """
    rows = {}
    with db() as conn:
        for r in conn.execute(
                "SELECT item, day, stock, capacity FROM market_stock_history "
                "WHERE market_id=? AND day >= date('now', ?) ORDER BY item, day",
                (str(market_id), f"-{int(days)} days")).fetchall():
            rows.setdefault(r["item"], []).append(dict(r))
    out = []
    for item, hist in rows.items():
        if len(hist) < 2:
            continue
        first, last = hist[0], hist[-1]
        try:
            from datetime import date as _d
            d0 = _d.fromisoformat(first["day"]); d1 = _d.fromisoformat(last["day"])
            span = (d1 - d0).days
        except Exception:
            span = 0
        if span <= 0:
            continue
        drop = float(first["stock"] or 0) - float(last["stock"] or 0)
        per_day = drop / span
        days_left = (float(last["stock"] or 0) / per_day) if per_day > 0 else None
        out.append({
            "item": item, "stock": int(last["stock"] or 0),
            "capacity": int(last["capacity"] or 0),
            "per_day": round(per_day, 1),
            "days_left": (round(days_left, 1) if days_left is not None else None),
            "readings": len(hist), "first_day": first["day"], "last_day": last["day"],
        })
    out.sort(key=lambda x: (x["days_left"] is None, x["days_left"] if x["days_left"] is not None else 1e9))
    return out


def get_hive_harvester_detail(market_id: str, ign: str) -> dict:
    """Item-level breakdown for ONE harvester on one hive site.

    Answers "how many comb blocks / honey blocks did this person actually deliver",
    which the aggregate unpaid-value figure can't. Returns:

    {"ign", "qty", "value", "paid_value", "unpaid_value", "first_sale", "last_sale",
     "items": {item: {"qty","unit_value","value","paid_qty","unpaid_qty",
                      "paid_value","unpaid_value"}}}
    """
    out = {"ign": str(ign), "qty": 0, "value": 0.0, "paid_value": 0.0,
           "unpaid_value": 0.0, "first_sale": None, "last_sale": None,
           "last_paid_at": None, "items": {}}
    with db() as conn:
        rows = conn.execute("""
            SELECT item,
                   SUM(qty)                                             AS qty,
                   MAX(unit_value)                                      AS unit_value,
                   SUM(qty * unit_value)                                AS value,
                   SUM(CASE WHEN paid=1 THEN qty ELSE 0 END)            AS paid_qty,
                   SUM(CASE WHEN paid=1 THEN qty*unit_value ELSE 0 END) AS paid_value,
                   MIN(COALESCE(sale_ts, recorded_at))                  AS first_sale,
                   MAX(COALESCE(sale_ts, recorded_at))                  AS last_sale,
                   MAX(paid_at)                                         AS last_paid_at
            FROM hive_harvests
            WHERE market_id=? AND ign=? COLLATE NOCASE
            GROUP BY item
            ORDER BY value DESC
        """, (str(market_id), str(ign))).fetchall()
    for r in rows:
        q = int(r["qty"] or 0)
        v = float(r["value"] or 0)
        pq = int(r["paid_qty"] or 0)
        pv = float(r["paid_value"] or 0)
        out["items"][r["item"]] = {
            "qty": q, "unit_value": float(r["unit_value"] or 0), "value": v,
            "paid_qty": pq, "unpaid_qty": q - pq,
            "paid_value": pv, "unpaid_value": v - pv,
        }
        out["qty"] += q
        out["value"] += v
        out["paid_value"] += pv
        out["unpaid_value"] += (v - pv)
        for key, val in (("first_sale", r["first_sale"]), ("last_sale", r["last_sale"]),
                         ("last_paid_at", r["last_paid_at"])):
            cur = out[key]
            if val and (cur is None or (val < cur if key == "first_sale" else val > cur)):
                out[key] = val
    return out


def add_land_entry(land: str, entry_no: int, ts: str, kind: str,
                   amount: float, new_balance, body: str) -> bool:
    """Store one land-inbox entry. Returns True if it was NEW.

    Dedup is by CONTENT — (land, timestamp, body) — not by the inbox position number.
    The land inbox is a rolling list where #30 is always the newest, so every new event
    shifts every older event's number down by one: the withdrawal that was #29 yesterday
    is #28 today. Keyed on entry_no (the old PRIMARY KEY (land, entry_no, ts)) every
    entry therefore looked new on every scan, so the whole backlog was re-stored under
    fresh numbers on each pass and the same $35,000 withdrawal could be counted several
    times over — which also fed the teleport-fee inference, since that walks the balance
    chain between consecutive entries.

    entry_no is still recorded, but only as "where it sat when we last saw it". It is
    deliberately not part of the identity of an entry.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT entry_no FROM land_ledger WHERE land=? AND ts=? AND body=?",
            (str(land), str(ts), str(body or "")[:300])).fetchone()
        if row is not None:
            # Same event, new position in the list — keep the row, refresh the position.
            if int(row[0] or 0) != int(entry_no):
                conn.execute(
                    "UPDATE land_ledger SET entry_no=? WHERE land=? AND ts=? AND body=?",
                    (int(entry_no), str(land), str(ts), str(body or "")[:300]))
            return False
        conn.execute("""
            INSERT OR IGNORE INTO land_ledger (land, entry_no, ts, kind, amount, new_balance, body)
            VALUES (?,?,?,?,?,?,?)
        """, (str(land), int(entry_no), str(ts), str(kind), float(amount),
              None if new_balance is None else float(new_balance), str(body or "")[:300]))
        return True


def get_land_entries(land: str) -> list[dict]:
    with db() as conn:
        # Order by the entry's own timestamp. entry_no shifts as the inbox rolls, so
        # ordering by it interleaves old and new events and corrupts the balance chain
        # that _recompute_fees walks. ts is "MM/DD/YYYY HH:MM" — rearrange to sort.
        rows = conn.execute(
            "SELECT * FROM land_ledger WHERE land=? "
            "ORDER BY substr(ts,7,4) || substr(ts,1,2) || substr(ts,4,2) "
            "         || substr(ts,12,5), entry_no",
            (str(land),)).fetchall()
        return [dict(r) for r in rows]


def set_land_balance(land: str, balance: float) -> None:
    with db() as conn:
        conn.execute("""
            INSERT INTO land_balances (land, balance, updated_at) VALUES (?,?,datetime('now'))
            ON CONFLICT(land) DO UPDATE SET balance=excluded.balance, updated_at=datetime('now')
        """, (str(land), float(balance)))


def get_all_config_prefixed(prefix: str) -> dict:
    """Every bot_config row whose key starts with prefix, as {key: value}. Used to answer
    reverse questions the schema can't — e.g. "which land maps to this market?", where the
    mapping is stored land-first (land_map:<land> -> market_id)."""
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM bot_config WHERE key LIKE ?",
                            (f"{prefix}%",)).fetchall()
        return {r["key"]: r["value"] for r in rows}


def get_land_balance(land: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM land_balances WHERE land=?", (str(land),)).fetchone()
        return dict(row) if row else None


def replace_land_fees(land: str, by_month: dict) -> None:
    """Replace the land's whole inferred-fee table (recomputed from scratch each
    ingest — idempotent, so re-scans and backfills can never double-count)."""
    with db() as conn:
        conn.execute("DELETE FROM land_fees WHERE land=?", (str(land),))
        for month, fees in (by_month or {}).items():
            conn.execute("INSERT INTO land_fees (land, month, fees) VALUES (?,?,?)",
                         (str(land), str(month), float(fees)))


def get_land_fees(land: str) -> dict:
    with db() as conn:
        rows = conn.execute("SELECT month, fees FROM land_fees WHERE land=?",
                            (str(land),)).fetchall()
        return {r["month"]: float(r["fees"] or 0) for r in rows}


def get_all_land_fees() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT land, month, fees FROM land_fees").fetchall()
        return [dict(r) for r in rows]


# ── Land Exchange (listings/auctions) ────────────────────────────────────────
_LAND_LISTING_FIELDS = (
    "seller_id", "kind", "title", "category", "photos",
    "market_id", "land", "chunks", "coords", "description",
    "image_url", "winner_message", "mode",
    "quality", "reserve", "buy_now", "current_bid", "current_bidder",
    "min_increment_pct", "commission_pct", "listing_fee", "starts_at", "ends_at",
    "anti_snipe_minutes", "status", "channel_id", "message_id", "sold_price",
    "sold_to", "closed_at",
    # Escrow progress markers (LAND_ESCROW_PLAN §2.1/§2.6). Listed here so
    # `update_land_listing` can write them; the CLAIMS on them are not written
    # through that function, because a claim needs a WHERE clause it does not have.
    "settle_stage", "settling_at", "fee_stage", "fee_paid",
)


def create_land_listing(**kwargs) -> int:
    """Insert a new listing. Recognised kwargs are any column in _LAND_LISTING_FIELDS
    (unset ones take the schema default). Returns the new listing's id."""
    cols = [k for k in kwargs if k in _LAND_LISTING_FIELDS]
    with db() as conn:
        cur = conn.execute(
            f"INSERT INTO land_listings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [kwargs[k] for k in cols],
        )
        return int(cur.lastrowid)


def get_land_listing(listing_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM land_listings WHERE id=?", (int(listing_id),)).fetchone()
        return dict(row) if row else None


def update_land_listing(listing_id: int, _if_status: str = None, **kwargs) -> int:
    """Partial update — only columns passed are touched. Always bumps updated_at.

    `_if_status` makes it a CLAIM: the write lands only while the row still says
    that status, and the return is the number of rows written (0 = we lost it, and
    the caller must not act as though it landed). Land's settle path needs this —
    an unconditional `status='sold'` overwrites whatever somebody else wrote
    underneath a settlement in flight, which is how a `rolled_back` lot came back
    as sold. Every other caller keeps the old unconditional behaviour by not
    passing it; the leading underscore keeps it out of the `_LAND_LISTING_FIELDS`
    namespace so it can never be mistaken for a column.
    """
    cols = [k for k in kwargs if k in _LAND_LISTING_FIELDS]
    if not cols:
        return 0
    set_clause = ", ".join(f"{c}=?" for c in cols) + ", updated_at=datetime('now')"
    sql = f"UPDATE land_listings SET {set_clause} WHERE id=?"
    params = [kwargs[k] for k in cols] + [int(listing_id)]
    if _if_status is not None:
        sql += " AND status=?"
        params.append(str(_if_status))
    with db() as conn:
        return conn.execute(sql, params).rowcount


def get_active_land_listings(mode: str = None) -> list[dict]:
    with db() as conn:
        if mode:
            rows = conn.execute(
                "SELECT * FROM land_listings WHERE status='active' AND mode=? ORDER BY "
                "(ends_at IS NULL), ends_at ASC, created_at DESC", (mode,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM land_listings WHERE status='active' ORDER BY "
                "(ends_at IS NULL), ends_at ASC, created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_land_listings_by_seller(seller_id: str, include_closed: bool = True) -> list[dict]:
    with db() as conn:
        if include_closed:
            rows = conn.execute(
                "SELECT * FROM land_listings WHERE seller_id=? ORDER BY created_at DESC",
                (str(seller_id),)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM land_listings WHERE seller_id=? AND status='active' "
                "ORDER BY created_at DESC", (str(seller_id),)).fetchall()
        return [dict(r) for r in rows]


def get_expired_active_listings() -> list[dict]:
    """Active auctions whose ends_at has passed — due for automatic settlement."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM land_listings WHERE status='active' AND ends_at IS NOT NULL "
            "AND ends_at <= datetime('now')").fetchall()
        return [dict(r) for r in rows]


def get_part_settled_active_listings(limit: int = 200) -> list[dict]:
    """Active lots whose ESCROW says somebody has already paid — due for RESUME.

    The companion to `get_expired_active_listings`, and deliberately NOT gated on
    `ends_at`. A lot with a `captured`, `capturing` or `capture_unknown` row is
    PART-SETTLED regardless of when it was due: its buyer's coins are in
    `treasury:estates` (or may be), the seller has not been paid, and the lot is
    still `active`. On a 7-day auction the deadline sweep never looked at it, so
    the only escape was the buyer clicking Buy again — and with
    `realestate:bidding_frozen` on, that escape is closed too. A ledger incident
    is both why the switch gets thrown and why captures get interrupted, so the
    two co-occur by construction.

    An interrupted instant buy is INVISIBLE on `land_listings` — it writes no
    `current_bid`/`current_bidder` — which is why this asks `land_bids` instead.

    A `held` row is selected ONLY when it is a BUY row (`kind='buy'`), because
    that is a purchase whose settlement did not finish rather than a bid. A
    standing auction bid is `kind` NULL/`bid` and is never selected here: an
    auction that has not reached `ends_at` must not be closed by this sweep. The
    caller decides what to do with the row; this only narrows the candidates so
    the sweep is one indexed EXISTS rather than a walk of every live lot.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT l.* FROM land_listings l WHERE l.status='active' AND EXISTS ("
            "SELECT 1 FROM land_bids b WHERE b.listing_id=l.id AND ("
            "b.status IN ('captured','capturing','capture_unknown') OR "
            "(b.status='held' AND b.kind='buy'))) ORDER BY l.id ASC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]


def add_land_bid(listing_id: int, bidder_id: str, amount: float, *,
                 kind: Optional[str] = None, hold_amount: Optional[int] = None,
                 status: Optional[str] = None) -> int:
    """Insert a bid row and return its id — the domain sequence number.

    That returned id is what every idempotency key for this bid is minted from
    (`land:listing:<lid>:bid:<id>`), which is why the row is written BEFORE any
    money call and why the id is not optional. It has always been returned here
    and `_place_bid_core` has always discarded it.

    The escrow kwargs are keyword-only and all default to None so the two-year-old
    call shape (`add_land_bid(lid, uid, amt)`) still means exactly what it meant:
    a display row, `status` taking the schema's `'legacy'` default, which no
    escrow sweep will ever touch.
    """
    cols = ["listing_id", "bidder_id", "amount"]
    vals: list[Any] = [int(listing_id), str(bidder_id), float(amount)]
    for name, val in (("kind", kind), ("hold_amount", hold_amount), ("status", status)):
        if val is not None:
            cols.append(name)
            vals.append(int(val) if name == "hold_amount" else str(val))
    with db() as conn:
        cur = conn.execute(
            f"INSERT INTO land_bids ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", vals)
        return int(cur.lastrowid)


def get_land_bids(listing_id: int, limit: int = 20) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM land_bids WHERE listing_id=? ORDER BY ts DESC LIMIT ?",
            (int(listing_id), limit)).fetchall()
        return [dict(r) for r in rows]


def claim_listing_stage(listing_id: int, expect: Optional[str], to: str) -> bool:
    """`settle_stage: expect -> to` in one atomic UPDATE. True only if we won it.

    `expect=None` claims a row whose stage has never been set, which is every
    listing created before this migration and every listing that has not started
    settling. `IS ?` is used rather than `= ?` so NULL compares equal — with `=`
    the first claim on every legacy listing silently matched nothing and the
    settle never started.
    """
    with db() as conn:
        cur = conn.execute(
            "UPDATE land_listings SET settle_stage=?, settling_at=datetime('now'), "
            "updated_at=datetime('now') WHERE id=? AND settle_stage IS ?",
            (str(to), int(listing_id), expect))
        return cur.rowcount == 1


def claim_listing_fee_stage(listing_id: int, expect: Optional[str], to: str) -> bool:
    """`fee_stage: expect -> to`, same contract as `claim_listing_stage`."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE land_listings SET fee_stage=?, updated_at=datetime('now') "
            "WHERE id=? AND fee_stage IS ?",
            (str(to), int(listing_id), expect))
        return cur.rowcount == 1


# ══════════════════════════════════════════════════════════════════════════
# Leases and rent
# ══════════════════════════════════════════════════════════════════════════

def create_land_lease(parcel_id: str, tenant_id: str, owner_id: str, amount: int,
                      *, period_days: int = 30, next_due_at: Optional[str] = None) -> int:
    """Record a rent agreement. Ownership is NOT recorded here — see the schema note."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO land_leases (parcel_id, tenant_id, owner_id, amount, "
            "period_days, next_due_at) VALUES (?,?,?,?,?,?)",
            (str(parcel_id), str(tenant_id), str(owner_id), int(amount),
             int(period_days), next_due_at))
        return int(cur.lastrowid)


def get_land_lease(lease_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM land_leases WHERE id=?", (int(lease_id),)).fetchone()
        return dict(row) if row else None


def land_leases_due(now_sql: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Active leases whose next payment is due. Read-only; the sweep decides."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM land_leases WHERE status='active' AND next_due_at IS NOT NULL "
            "AND next_due_at <= COALESCE(?, datetime('now')) ORDER BY id ASC LIMIT ?",
            (now_sql, int(limit))).fetchall()
        return [dict(r) for r in rows]


def open_rent_charge(lease: dict, period: str, idem_key: str) -> Optional[int]:
    """Create the charge row for one (parcel, period), or None if it already exists.

    THE FIRST OF THE THREE THINGS THAT STOP A DOUBLE CHARGE, and the only one that
    works before any code has run: `idx_land_rent_period` is UNIQUE on
    `(parcel_id, period)`, so a second attempt to bill February cannot be written
    down at all, whatever the sweep believes about its own progress. The other two
    are the row claim (`claim_rent_charge`) and the ledger key — three
    independent mechanisms, because rent is a scheduled job and a scheduled job
    is retried by things nobody remembers configuring.
    """
    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO land_rent_charges (lease_id, parcel_id, period, tenant_id, "
                "owner_id, amount, idem_key) VALUES (?,?,?,?,?,?,?)",
                (int(lease["id"]), str(lease["parcel_id"]), str(period),
                 str(lease["tenant_id"]), str(lease["owner_id"]),
                 int(lease["amount"]), str(idem_key)))
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def get_rent_charge(charge_id: int) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM land_rent_charges WHERE id=?",
                           (int(charge_id),)).fetchone()
        return dict(row) if row else None


def find_rent_charge(parcel_id: str, period: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM land_rent_charges WHERE parcel_id=? AND period=?",
            (str(parcel_id), str(period))).fetchone()
        return dict(row) if row else None


def claim_rent_charge(charge_id: int) -> Optional[dict]:
    """`pending -> claimed` in one atomic UPDATE. Returns the row iff WE won it."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE land_rent_charges SET status='claimed', claimed_at=datetime('now'), "
            "attempts=attempts+1 WHERE id=? AND status='pending'", (int(charge_id),))
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM land_rent_charges WHERE id=?",
                           (int(charge_id),)).fetchone()
        return dict(row) if row else None


def settle_rent_charge(charge_id: int, *, ledger_ref: Optional[str] = None,
                       replayed: bool = False) -> bool:
    """`claimed -> paid`, and advance the lease's period marker in the SAME transaction.

    The two writes are one transaction because they are one fact. If they were
    two, a crash between them leaves a paid charge and a lease that still thinks
    the period is owed — and the next sweep bills it again. Committing together
    means: if this returns True the parcel can never be charged for that period
    again; if it does not commit, the charge stays `claimed` and is retried,
    where the ledger key makes the retry a replay rather than a payment.
    """
    with db() as conn:
        row = conn.execute("SELECT * FROM land_rent_charges WHERE id=?",
                           (int(charge_id),)).fetchone()
        if row is None:
            return False
        cur = conn.execute(
            "UPDATE land_rent_charges SET status='paid', settled_at=datetime('now'), "
            "ledger_ref=?, replayed=?, last_error=NULL WHERE id=? AND status='claimed'",
            (ledger_ref, 1 if replayed else 0, int(charge_id)))
        if cur.rowcount != 1:
            return False
        conn.execute(
            "UPDATE land_leases SET last_period=?, "
            "next_due_at=datetime(COALESCE(next_due_at, datetime('now')), "
            "                     '+' || period_days || ' days'), "
            "updated_at=datetime('now') WHERE id=?",
            (row["period"], int(row["lease_id"])))
        return True


def release_rent_charge(charge_id: int, error: str, *, permanent: bool = False,
                        max_attempts: int = 5) -> None:
    """Hand the row back: transient -> `pending` (retried), permanent -> `failed`.

    A row that has burned `max_attempts` parks as `failed` rather than being
    retried once a minute forever. Parked is visible; a hot loop against a
    permanently-refused charge is not.
    """
    with db() as conn:
        row = conn.execute("SELECT attempts FROM land_rent_charges WHERE id=?",
                           (int(charge_id),)).fetchone()
        if row is None:
            return
        exhausted = int(row["attempts"] or 0) >= int(max_attempts)
        conn.execute(
            "UPDATE land_rent_charges SET status=?, last_error=? WHERE id=? AND status='claimed'",
            ("failed" if (permanent or exhausted) else "pending",
             str(error)[:500], int(charge_id)))


def park_rent_charge_unknown(charge_id: int, error: str) -> None:
    """The outcome is unknown, so the row goes to `unknown` and NOT back to `pending`.

    A `pending` row is one the sweep will charge. An unknown one may already have
    been charged, so it must be asked about, not retried blind. Same rule as
    `LAND_BID_UNKNOWN`, for the same reason.
    """
    with db() as conn:
        conn.execute(
            "UPDATE land_rent_charges SET status='unknown', last_error=? "
            "WHERE id=? AND status='claimed'", (str(error)[:500], int(charge_id)))


def delete_note(note_id: int):
    """Delete a note by ID."""
    with db() as conn:
        conn.execute("DELETE FROM notes WHERE id=?", (note_id,))



def get_market_shares(market_id: str) -> Optional[dict]:
    """Return the stock-listing row for a market (public or delisted), or None
    if it has never gone public."""
    with db() as conn:
        row = conn.execute("SELECT * FROM market_shares WHERE market_id=?", (market_id,)).fetchone()
        return dict(row) if row else None


def get_public_markets() -> dict:
    """Return {market_id: dict} for markets currently listed (active=1)."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM market_shares WHERE active=1").fetchall()
        return {row["market_id"]: dict(row) for row in rows}


def get_all_market_shares() -> dict:
    """Return {market_id: dict} for every market that has ever gone public,
    public or delisted."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM market_shares").fetchall()
        return {row["market_id"]: dict(row) for row in rows}


def upsert_market_shares(market_id: str, **kwargs) -> dict:
    """Create or update a market's stock listing. Any field not passed (or
    passed as None) keeps its current value — or the schema default if this
    is a brand-new listing. Returns the resulting row.

    Recognised kwargs: active, shares_outstanding, pe_multiplier, share_price,
    last_priced_at, last_priced_month, dividend_pct, last_dividend_month.

    `treasury_coins` IS NOT ONE OF THEM, and the refusal below is deliberate.
    This function's update arm is a full-row read-modify-write: it SELECTs the
    row, builds a value dict from that read, and stores it back. For the
    listing's descriptive fields that is harmless — the last writer of a price
    genuinely is the right price. For `treasury_coins` it is a LOST UPDATE, and
    `treasury_coins` is the pot the whole markets engine's coins live in.

    Measured on the shape this refusal removes: `_persist_price` called this on
    the success path of every buy and every sell, immediately after the trade's
    own transaction committed, so every trade rewrote the treasury from a read
    it took after its own commit. Eight concurrent sells against a 30,000-coin
    treasury MINTED +29,625; eight concurrent buys DESTROYED 60,261 of the
    80,824 those buyers had paid in; a dividend run racing four sells minted
    +27,653. Nothing errored, every buyer got their shares, and the treasury
    figure was simply wrong.

    The treasury has exactly two writers now, and neither of them is here:
      * `adjust_treasury` — relative, claim-first, returns what it applied.
        Every money path uses this.
      * `set_market_treasury_absolute` — the staff override behind
        `/market treasury set`, which is absolute because "store the number I
        typed" is what was asked for. No money path reaches it, and
        `tests/test_money_tx_contract.py` section 7e asserts that mechanically
        rather than taking a waiver's word for it.
    """
    if kwargs.get("treasury_coins") is not None:
        raise ValueError(
            "upsert_market_shares does not write treasury_coins — it would be a "
            "lost update against adjust_treasury. Use adjust_treasury(...) for a "
            "delta, or set_market_treasury_absolute(...) for the staff override."
        )
    with db() as conn:
        existing_row = conn.execute(
            "SELECT * FROM market_shares WHERE market_id=?", (market_id,)
        ).fetchone()
        existing = dict(existing_row) if existing_row else {}

        def field(key, default):
            if key in kwargs and kwargs[key] is not None:
                return kwargs[key]
            return existing.get(key, default)

        values = {
            "mid": market_id,
            "active": int(field("active", 1)),
            "shares": float(field("shares_outstanding", 1000.0)),
            "pe": float(field("pe_multiplier", 12.0)),
            "price": float(field("share_price", 0.0)),
            "listed_at": existing.get("listed_at") or datetime.now(timezone.utc).isoformat(),
            "last_priced_at": field("last_priced_at", None),
            "last_priced_month": field("last_priced_month", None),
            # INSERT arm only — the seed for a listing that does not exist yet.
            # The DO UPDATE arm below deliberately does not carry it across.
            "treasury": float(existing.get("treasury_coins") or 0.0),
            "div_pct": field("dividend_pct", None),
            "last_div_month": field("last_dividend_month", None),
        }
        conn.execute("""
            INSERT INTO market_shares (
                market_id, active, shares_outstanding, pe_multiplier, share_price,
                listed_at, last_priced_at, last_priced_month,
                treasury_coins, dividend_pct, last_dividend_month
            )
            VALUES (
                :mid, :active, :shares, :pe, :price,
                :listed_at, :last_priced_at, :last_priced_month,
                :treasury, :div_pct, :last_div_month
            )
            ON CONFLICT(market_id) DO UPDATE SET
                active=excluded.active,
                shares_outstanding=excluded.shares_outstanding,
                pe_multiplier=excluded.pe_multiplier,
                share_price=excluded.share_price,
                last_priced_at=excluded.last_priced_at,
                last_priced_month=excluded.last_priced_month,
                dividend_pct=excluded.dividend_pct,
                last_dividend_month=excluded.last_dividend_month
        """, values)
        row = conn.execute("SELECT * FROM market_shares WHERE market_id=?", (market_id,)).fetchone()
        return dict(row)


def set_market_treasury_absolute(market_id: str, coins: float) -> bool:
    """THE STAFF OVERRIDE, and the only absolute write to `treasury_coins` in the
    tree. `/market treasury set` reads the old figure, shows `old -> new` on
    screen and stores exactly what was typed, so an absolute write is what was
    asked for — there is no delta to apply.

    It lives in a function of its own rather than as a kwarg of
    `upsert_market_shares` for one reason: an absolute write to an accumulator
    is only safe if nothing that moves coins can reach it, and "nothing that
    moves coins can reach it" is a property of a FUNCTION, not of a keyword
    argument. As a kwarg it was reachable from `_persist_price`, on the success
    path of every trade, while a hand-written waiver in the contract test said
    the trading paths never came through there. Split out, the claim is
    mechanically checkable and `tests/test_money_tx_contract.py` section 7e
    checks it: `ABSOLUTE_OK` must name every money-path function that reaches
    this, and today that list is empty.

    THE RESIDUAL, STATED RATHER THAN HIDDEN: this is still a lost update if an
    admin types a figure while a trade is landing — measured at 9,653-19,306
    coins created, 3/3 runs. That is accepted, because a human is choosing the
    number with the market in front of them; it is not accepted silently, and
    the command's confirmation says so.

    Returns True if a listing was updated, False if there is no such row.
    """
    with db() as conn:
        cur = conn.execute(
            "UPDATE market_shares SET treasury_coins=? WHERE market_id=?",
            (float(coins), market_id),
        )
        return cur.rowcount > 0


def get_holding(user_id: str, market_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM stock_holdings WHERE user_id=? AND market_id=?",
            (str(user_id), market_id),
        ).fetchone()
        return dict(row) if row else None


def get_portfolio(user_id: str) -> list[dict]:
    """All of a user's holdings (shares > 0), across every market."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_holdings WHERE user_id=? AND shares > 0 ORDER BY market_id",
            (str(user_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_holders(market_id: str) -> list[dict]:
    """All current holders (shares > 0) of a given market's stock."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_holdings WHERE market_id=? AND shares > 0 ORDER BY shares DESC",
            (market_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def adjust_holding(user_id: str, market_id: str, delta_shares: float,
                   delta_cost_basis: float, *, conn=None):
    """Apply a buy (+shares/+cost) or sell (-shares/-cost) to a user's holding,
    creating the row if needed. Caller is responsible for checking that a sell
    doesn't take shares negative.

    Pass `conn=` to run inside the caller's transaction, so the share movement and
    the coin movement that pays for it commit together (see `db_in`). A sell that
    wants the share claim itself to be the gate should use `claim_holding_tx`,
    which refuses on the rowcount instead of trusting a preceding read."""
    now = datetime.now(timezone.utc).isoformat()
    with db_in(conn) as conn:
        conn.execute("""
            INSERT INTO stock_holdings (user_id, market_id, shares, cost_basis, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, market_id) DO UPDATE SET
                shares=shares + excluded.shares,
                cost_basis=cost_basis + excluded.cost_basis,
                updated_at=excluded.updated_at
        """, (str(user_id), market_id, delta_shares, delta_cost_basis, now))


def claim_holding_tx(conn, user_id: str, market_id: str, shares: float,
                     cost_basis_removed: float) -> bool:
    """CLAIM-FIRST SELL: remove `shares` from a holding in ONE atomic UPDATE gated
    on the believed state (`shares >= ?`), and return whether we won the row.

    The read-then-write form this replaces (`get_holding` -> compare ->
    `adjust_holding(-shares)`) is only safe because every caller happens to be
    serialized on the bot's event loop today. That is an accident of deployment,
    not an invariant, and the accident is load-bearing for a money path: two
    concurrent sells of the same 100 shares both pass the read and both get paid,
    and the holding goes negative with no error. Here the WHERE clause is the
    check, the rowcount is the answer, and losing the race is a clean refusal.

    Returns False without touching anything if the holder no longer has the
    shares. Must be called inside the caller's transaction — the whole point is
    that the share claim and the coin credit commit together."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE stock_holdings SET shares = shares - ?, cost_basis = cost_basis - ?, "
        "updated_at = ? WHERE user_id = ? AND market_id = ? AND shares >= ?",
        (float(shares), float(cost_basis_removed), now,
         str(user_id), str(market_id), float(shares)))
    return cur.rowcount > 0


def log_stock_trade(user_id: str, market_id: str, side: str, shares: float,
                     price_per_share: float, total_coins: float, *, conn=None):
    """Append the trade to the audit log. Pass `conn=` to write it in the same
    transaction as the trade, so "the coins moved but no trade was logged" stops
    being a reachable state."""
    with db_in(conn) as conn:
        conn.execute("""
            INSERT INTO stock_trade_log (user_id, market_id, side, shares, price_per_share, total_coins)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(user_id), market_id, side, shares, price_per_share, total_coins))


def get_trade_log(market_id: str = None, user_id: str = None, limit: int = 20) -> list[dict]:
    with db() as conn:
        if market_id and user_id:
            rows = conn.execute(
                "SELECT * FROM stock_trade_log WHERE market_id=? AND user_id=? "
                "ORDER BY traded_at DESC LIMIT ?",
                (market_id, str(user_id), limit),
            ).fetchall()
        elif market_id:
            rows = conn.execute(
                "SELECT * FROM stock_trade_log WHERE market_id=? ORDER BY traded_at DESC LIMIT ?",
                (market_id, limit),
            ).fetchall()
        elif user_id:
            rows = conn.execute(
                "SELECT * FROM stock_trade_log WHERE user_id=? ORDER BY traded_at DESC LIMIT ?",
                (str(user_id), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_trade_log ORDER BY traded_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def log_stock_price(market_id: str, price: float, reason: str = None):
    with db() as conn:
        conn.execute(
            "INSERT INTO stock_price_log (market_id, price, reason) VALUES (?, ?, ?)",
            (market_id, price, reason),
        )


def get_price_history(market_id: str, limit: int = 30) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_price_log WHERE market_id=? ORDER BY logged_at DESC LIMIT ?",
            (market_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]



def get_treasury(market_id: str) -> float:
    with db() as conn:
        row = conn.execute(
            "SELECT treasury_coins FROM market_shares WHERE market_id=?", (market_id,)
        ).fetchone()
        return float(row["treasury_coins"] or 0.0) if row else 0.0


def adjust_treasury(market_id: str, delta: float, allow_negative: bool = True,
                    *, conn=None) -> float:
    """CLAIM-FIRST TREASURY. Add (delta>0, e.g. a buy paying in) or remove
    (delta<0, e.g. funding a sell) coins from a market's treasury in ONE relative
    UPDATE gated on the believed state, and return the delta that was ACTUALLY
    applied — read from the rowcount, never from a preceding SELECT.

    WHY THIS IS NOT READ-THEN-WRITE ANY MORE
    ----------------------------------------
    This was `SELECT treasury_coins` -> compute -> `UPDATE treasury_coins = <abs>`,
    and every one of its neighbours (`adjust_balance_tx`, `claim_holding_tx`,
    `dividend_leg_claim`) was made relative and claim-first while this one was
    not. It is the primitive all of them rest on, so the miss was load-bearing:

      * two concurrent sells against a treasury that can fund one both passed the
        `allow_negative=False` check and both were paid — 9,653 coins MINTED from
        nothing, measured, deterministic over three runs;
      * twenty concurrent `+1000` credits left the treasury holding 3,000 —
        17,000 coins DESTROYED, with zero errors, because an absolute UPDATE
        computed from a stale read silently discards every delta it did not see.

    Nothing errored in either case, which is the point: a lost update is not an
    exception, it is a wrong number. The `SELECT` also ran OUTSIDE the write
    transaction (sqlite3's legacy isolation opens the transaction at the first
    DML statement, not the first read), so SQLite had nothing to complain about.

    The sell path's whole correctness rests on reading `applied` back from here:
    the unfunded-sell mint was closed by claiming the treasury BEFORE touching
    the holding and refusing when the claim came up short. If the claim itself is
    not atomic, that fix is standing on sand. Now the WHERE clause is the check
    and the rowcount is the answer.

    `allow_negative` KEEPS ITS MEANING. True (the default) lets the treasury go
    negative — an admin correction, a compensating move — and always applies the
    full delta. False refuses to take the treasury below zero and DRAWS DOWN TO
    ZERO instead, returning the smaller amount actually taken, so a caller can
    tell a full claim from a short one and say how short. Every live
    `allow_negative=False` caller refuses on that number rather than part-paying,
    and the short figure is what tells a refused seller the size that WOULD work.

    Returns 0.0 when there is no `market_shares` row (delisted): `action_log`
    distinguishes "the correction applied" from "the market is gone" on exactly
    that, and a 0-row UPDATE gives the same answer the old missing-row SELECT did.

    Pass `conn=` to run inside the caller's transaction, so the treasury claim and
    the credit it funds commit together (see `db_in`)."""
    d = float(delta)
    with db_in(conn) as conn:
        # The claim: relative, guarded, all-or-nothing. The guard only bites when
        # coins are LEAVING and the caller asked not to go negative.
        cur = conn.execute(
            "UPDATE market_shares SET treasury_coins = treasury_coins + ? "
            "WHERE market_id=? AND (? OR ? >= 0 OR treasury_coins + ? >= 0)",
            (d, market_id, 1 if allow_negative else 0, d, d))
        if cur.rowcount:
            return d
        if allow_negative or d >= 0:
            return 0.0                      # no such market_shares row
        # Short treasury, and the caller allows a partial draw-down: the result
        # is "take what is there", so the amount taken has to be read. It is read
        # inside the transaction the UPDATE above opened, and the drain is ITSELF
        # claim-first — gated on the exact value just read — so a concurrent
        # writer either loses the race (rowcount 0 -> 0.0, a clean refusal) or
        # invalidates our snapshot and SQLite raises. Neither one part-pays
        # against a stale number, which is the only outcome that could mint.
        row = conn.execute(
            "SELECT treasury_coins FROM market_shares WHERE market_id=?", (market_id,)
        ).fetchone()
        if not row:
            return 0.0
        held = float(row["treasury_coins"] or 0.0)
        if held <= 0:
            return 0.0
        took = conn.execute(
            "UPDATE market_shares SET treasury_coins = 0 "
            "WHERE market_id=? AND treasury_coins = ?", (market_id, held))
        return -held if took.rowcount else 0.0



def add_limit_order(user_id: str, market_id: str, side: str, shares: int,
                    limit_price: float, note: str = None) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO stock_limit_orders (user_id, market_id, side, shares, limit_price, note) "
            "VALUES (?,?,?,?,?,?)",
            (str(user_id), market_id, side, int(shares), float(limit_price), note),
        )
        return int(cur.lastrowid)


def get_limit_order(order_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM stock_limit_orders WHERE id=?", (int(order_id),)
        ).fetchone()
        return dict(row) if row else None


def get_open_limit_orders(market_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_limit_orders WHERE market_id=? AND status='open' ORDER BY id",
            (market_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_limit_orders(user_id: str, include_resolved: bool = False) -> list[dict]:
    with db() as conn:
        if include_resolved:
            rows = conn.execute(
                "SELECT * FROM stock_limit_orders WHERE user_id=? ORDER BY id DESC LIMIT 50",
                (str(user_id),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_limit_orders WHERE user_id=? AND status='open' ORDER BY id DESC",
                (str(user_id),),
            ).fetchall()
        return [dict(r) for r in rows]


def mark_limit_order_filled(order_id: int, fill_price: float, fill_total: float) -> bool:
    """Resolve an OPEN order to `filled`. Returns True if THIS caller won the row.

    It used to return None and discard the rowcount. The UPDATE is conditional on
    `status='open'`, so an order already resolved by another pass silently
    changed nothing while the caller — which has just EXECUTED A REAL TRADE —
    carried on as though the order were now closed. A claim whose answer nobody
    reads is not a claim; the trade is what needs the receipt."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE stock_limit_orders SET status='filled', fill_price=?, fill_total=?, "
            "resolved_at=datetime('now') WHERE id=? AND status='open'",
            (float(fill_price), float(fill_total), int(order_id)),
        )
        return cur.rowcount > 0


def note_limit_order_refusal(order_id: int, reason: str) -> int:
    """Record that this OPEN order triggered and was refused, and return how many
    times it now has. 0 means the order was not open — nothing was recorded.

    THE THIRD OUTCOME. `_check_limit_orders` had two: fill it, or cancel it on a
    terminal refusal. A `no_liquidity` or `credit_refused` was neither — the
    order stayed open, nothing was written down, and it was retried on every
    price tick for ever. Silently, because there was no log line either. This is
    where that outcome goes, so "still open, refused 40 times, no_liquidity" is a
    thing an operator can see and the order can eventually give up.

    Claim-first: one relative UPDATE gated on `status='open'`, and the count is
    read back inside the same transaction. It is not merely tidiness — the caller
    CANCELS on the number this returns, so a lost increment is an order that
    never gives up."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE stock_limit_orders SET refusals = refusals + 1, last_refusal=?, "
            "last_refused_at=datetime('now') WHERE id=? AND status='open'",
            (str(reason or "")[:80], int(order_id)))
        if not cur.rowcount:
            return 0
        row = conn.execute("SELECT refusals FROM stock_limit_orders WHERE id=?",
                           (int(order_id),)).fetchone()
        return int(row["refusals"]) if row else 0


def get_refused_limit_orders(min_refusals: int = 1, limit: int = 50) -> list[dict]:
    """Open orders that have triggered and been refused — the operator's view of
    "why is this order still sitting there". Newest refusal first."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_limit_orders WHERE status='open' AND refusals >= ? "
            "ORDER BY last_refused_at DESC, id DESC LIMIT ?",
            (int(min_refusals), int(limit))).fetchall()
        return [dict(r) for r in rows]


def cancel_limit_order(order_id: int, user_id: str = None, reason: str = None) -> bool:
    """Cancel an OPEN order. If user_id is given, only cancel the order when it
    belongs to that user. Returns True if a row was changed."""
    with db() as conn:
        if user_id is not None:
            cur = conn.execute(
                "UPDATE stock_limit_orders SET status='cancelled', note=COALESCE(?, note), "
                "resolved_at=datetime('now') WHERE id=? AND status='open' AND user_id=?",
                (reason, int(order_id), str(user_id)),
            )
        else:
            cur = conn.execute(
                "UPDATE stock_limit_orders SET status='cancelled', note=COALESCE(?, note), "
                "resolved_at=datetime('now') WHERE id=? AND status='open'",
                (reason, int(order_id)),
            )
        return cur.rowcount > 0



def log_dividend(market_id: str, month: str, total_paid: float,
                 per_share: float, holders: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO stock_dividend_log (market_id, month, total_paid, per_share, holders) "
            "VALUES (?,?,?,?,?)",
            (market_id, month, float(total_paid), float(per_share), int(holders)),
        )


# ── Dividend runs: per-holder progress markers ──────────────────────────────
#
# THE RULE THESE EXIST TO ENFORCE: the marker for a holder is committed BEFORE
# that holder's credit is attempted. Everything else here follows from that.

def dividend_run_id(market_id: str, month: str, source: str = "auto") -> str:
    return f"div:{market_id}:{month}:{source}"


def dividend_run_open(market_id: str, month: str, source: str, plan: list,
                      *, pool: int, per_share: float,
                      charge_treasury: bool = True) -> dict:
    """Open (or re-attach to) the run for this (market, month, source).

    `plan` is [(user_id, shares, amount)]. THE FIRST PLAN WINS. If a run already
    exists its legs are returned untouched, because those legs are what a crashed
    predecessor already acted on — re-planning from today's holder list would let
    somebody who bought shares after the crash collect a dividend the run was
    never sized for, and would silently drop a holder who has since sold and is
    genuinely owed their leg. The plan is pinned exactly as split_rules pins its
    leg expansion, and for the same reason.

    Returns {run_id, resumed, legs:[{user_id,shares,amount,state,detail}], ...}."""
    rid = dividend_run_id(market_id, month, source)
    with db() as conn:
        row = conn.execute("SELECT * FROM stock_dividend_runs WHERE run_id=?",
                           (rid,)).fetchone()
        resumed = row is not None
        if row is None:
            conn.execute(
                "INSERT INTO stock_dividend_runs (run_id, market_id, month, source, pool, "
                " per_share, holders, charge_treasury) VALUES (?,?,?,?,?,?,?,?)",
                (rid, str(market_id), str(month), str(source), int(pool), float(per_share),
                 len(plan), 1 if charge_treasury else 0))
            for uid, sh, amt in plan:
                if int(amt) <= 0:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO stock_dividend_legs (run_id, user_id, shares, amount) "
                    "VALUES (?,?,?,?)", (rid, str(uid), float(sh), int(amt)))
            row = conn.execute("SELECT * FROM stock_dividend_runs WHERE run_id=?",
                               (rid,)).fetchone()
        legs = [dict(r) for r in conn.execute(
            "SELECT * FROM stock_dividend_legs WHERE run_id=? ORDER BY amount DESC, user_id",
            (rid,)).fetchall()]
    out = dict(row)
    out["resumed"] = resumed
    out["legs"] = legs
    return out


def dividend_leg_claim(run_id: str, user_id: str, *, conn=None) -> int:
    """Claim-first: mark this holder's leg 'claimed' and COMMIT, before any coin
    moves. Returns the leg amount if we won the row, else 0.

    One atomic UPDATE gated on the believed state, and the ROWCOUNT is what
    decides — not a preceding SELECT. A leg already claimed/applied/unknown by
    another attempt returns 0 and is skipped, which is the whole double-pay
    defence.

    Pass `conn=` to claim inside the caller's transaction. That is a STRONGER
    guarantee than the separate commit, not a weaker one: with the claim, the
    treasury debit and the credit in one transaction there is no window in which
    a leg is claimed and unpaid, so a process death leaves it 'planned' and
    retryable rather than 'claimed' and UNKNOWN. `dividend_run_adopt_stale_claims`
    then has nothing to adopt on the closed path — it stays because legs written
    by an older build, or by the `conn=None` form, can still be sitting there."""
    with db_in(conn) as conn:
        cur = conn.execute(
            "UPDATE stock_dividend_legs SET state='claimed', updated_at=datetime('now') "
            "WHERE run_id=? AND user_id=? AND state='planned'",
            (str(run_id), str(user_id)))
        if not cur.rowcount:
            return 0
        row = conn.execute(
            "SELECT amount FROM stock_dividend_legs WHERE run_id=? AND user_id=?",
            (str(run_id), str(user_id))).fetchone()
        return int(row["amount"]) if row else 0


def dividend_leg_settle(run_id: str, user_id: str, state: str, detail: str = "",
                        *, conn=None) -> bool:
    """Resolve a claimed leg to applied / refused / unknown. Returns True if it moved.

    Pass `conn=` to settle in the same transaction as the money it describes."""
    if state not in ("applied", "refused", "unknown"):
        raise ValueError(f"bad dividend leg state {state!r}")
    with db_in(conn) as conn:
        cur = conn.execute(
            "UPDATE stock_dividend_legs SET state=?, detail=?, updated_at=datetime('now') "
            "WHERE run_id=? AND user_id=? AND state='claimed'",
            (state, str(detail)[:400], str(run_id), str(user_id)))
        return cur.rowcount > 0


def dividend_leg_state(run_id: str, user_id: str) -> Optional[str]:
    """The leg's current state, or None if it cannot be read.

    This is how an UNKNOWN gets RESOLVED rather than merely recorded. Once the
    leg marker and the coin credit commit in ONE transaction, the marker is no
    longer a hint about the money — it IS the money's receipt. So a caller whose
    `commit()` failed ambiguously can come back afterwards and ask: 'applied'
    means the credit is on disk, 'planned' means the whole transaction rolled
    back and nothing moved. Only an unreadable database leaves a genuine UNKNOWN,
    and that is why this returns None instead of guessing."""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT state FROM stock_dividend_legs WHERE run_id=? AND user_id=?",
                (str(run_id), str(user_id))).fetchone()
            return str(row["state"]) if row else None
    except Exception:
        return None


def dividend_leg_mark_unknown(run_id: str, user_id: str, detail: str = "") -> bool:
    """Park a leg in `unknown` from EITHER 'planned' or 'claimed'.

    `dividend_leg_settle` only moves a leg out of 'claimed', which is right for
    the normal path. This exists for the one case that atomicity cannot answer:
    the database could not be re-read at all, so the leg's own state is no help.
    Deliberately gated on `state IN ('planned','claimed')` so it can never
    overwrite an 'applied' leg — writing "we don't know" over a receipt would
    turn a paid holder into a candidate for being paid again."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE stock_dividend_legs SET state='unknown', detail=?, "
            "updated_at=datetime('now') WHERE run_id=? AND user_id=? "
            "AND state IN ('planned','claimed')",
            (str(detail)[:400], str(run_id), str(user_id)))
        return cur.rowcount > 0


def dividend_run_adopt_stale_claims(run_id: str) -> int:
    """Any leg still 'claimed' when a run STARTS belongs to an attempt that died
    holding it. Its outcome is UNKNOWN — the credit may or may not have landed —
    so it is recorded as unknown and never automatically re-credited. Re-crediting
    is precisely the mint this whole mechanism exists to stop; the cost of the
    honest answer is that a human has to look at these. Returns how many."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE stock_dividend_legs SET state='unknown', updated_at=datetime('now'), "
            " detail=CASE WHEN detail='' THEN 'claimed by an attempt that did not finish; "
            "outcome unknown — check the coin ledger before paying' ELSE detail END "
            "WHERE run_id=? AND state='claimed'", (str(run_id),))
        return int(cur.rowcount or 0)


def dividend_run_rearm_refused(run_id: str) -> int:
    """Put `refused` legs back to `planned` at the START of a fresh attempt.

    `refused` is the DEFINITE negative — the credit provably did not happen and
    any treasury coins taken for it were put back — so the holder is still owed
    and a later attempt must pick them up. Re-arming happens once, when an attempt
    begins, and never inside its own loop: a leg that fails again this pass is
    refused again and waits for the next one, instead of spinning. `unknown` is
    deliberately NOT re-armed; that is the whole point of having a third state."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE stock_dividend_legs SET state='planned', updated_at=datetime('now') "
            "WHERE run_id=? AND state='refused'", (str(run_id),))
        return int(cur.rowcount or 0)


def dividend_run_legs(run_id: str, state: str = None) -> list:
    with db() as conn:
        if state:
            rows = conn.execute(
                "SELECT * FROM stock_dividend_legs WHERE run_id=? AND state=? "
                "ORDER BY amount DESC, user_id", (str(run_id), str(state))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_dividend_legs WHERE run_id=? ORDER BY amount DESC, user_id",
                (str(run_id),)).fetchall()
        return [dict(r) for r in rows]


def dividend_run_tally(run_id: str) -> dict:
    """{paid, counts:{state:n}, unresolved} — `paid` sums ONLY applied legs, so
    the treasury is debited by what definitely reached wallets and nothing else."""
    with db() as conn:
        rows = conn.execute(
            "SELECT state, COUNT(*) n, COALESCE(SUM(amount),0) amt "
            "FROM stock_dividend_legs WHERE run_id=? GROUP BY state", (str(run_id),)).fetchall()
    counts = {r["state"]: int(r["n"]) for r in rows}
    amounts = {r["state"]: int(r["amt"]) for r in rows}
    unresolved = sum(counts.get(s, 0) for s in ("planned", "claimed", "unknown", "refused"))
    return {"paid": amounts.get("applied", 0), "counts": counts,
            "amounts": amounts, "unresolved": unresolved}


def dividend_run_close(run_id: str, *, paid: int, treasury_charged: int,
                       complete: bool) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE stock_dividend_runs SET state=?, paid=?, treasury_charged=?, "
            " settled_at=datetime('now') WHERE run_id=?",
            ("complete" if complete else "partial", int(paid), int(treasury_charged),
             str(run_id)))


def dividend_runs_unfinished(limit: int = 50) -> list:
    """Runs that are not complete — the operator surface for "who is still owed a
    dividend, and whose outcome nobody knows"."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM stock_dividend_runs WHERE state<>'complete' "
            "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            t = conn.execute(
                "SELECT state, COUNT(*) n FROM stock_dividend_legs WHERE run_id=? GROUP BY state",
                (d["run_id"],)).fetchall()
            d["leg_counts"] = {x["state"]: int(x["n"]) for x in t}
            out.append(d)
        return out


def dividend_paid(market_id: str, month: str) -> bool:
    """PERMANENT per-month idempotency for share dividends. The old guard was a
    single last_dividend_month slot, so re-importing any OLD month (a routine
    earnings-correction workflow) double-paid every shareholder. The dividend
    log keeps one row per (market, month) forever — this is the authoritative
    'was it paid' check."""
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM stock_dividend_log WHERE market_id=? AND month=? LIMIT 1",
            (str(market_id), str(month))).fetchone()
        return row is not None


def get_dividend_history(market_id: str = None, limit: int = 36) -> list:
    """Dividend rows, newest first. market_id=None returns every market's — the investor
    page needs the whole series to show what a holding actually paid over time, which
    get_last_dividend (one row) could never answer."""
    with db() as conn:
        if market_id:
            rows = conn.execute(
                "SELECT * FROM stock_dividend_log WHERE market_id=? ORDER BY month DESC, id DESC "
                "LIMIT ?", (str(market_id), int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_dividend_log ORDER BY month DESC, id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]


def get_last_dividend(market_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM stock_dividend_log WHERE market_id=? ORDER BY id DESC LIMIT 1",
            (market_id,),
        ).fetchone()
        return dict(row) if row else None
