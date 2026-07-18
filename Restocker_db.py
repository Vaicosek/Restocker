"""
db.py — SQLite database layer for Restocker bot.
Replaces all YAML file I/O with a single restocker.db file.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path("restocker.db")

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
CREATE TABLE IF NOT EXISTS csn_history_items (
    market_id   TEXT NOT NULL DEFAULT 'main',
    month       TEXT NOT NULL,
    item        TEXT NOT NULL,
    sold_qty    INTEGER NOT NULL DEFAULT 0,
    bought_qty  INTEGER NOT NULL DEFAULT 0,
    net_coins   REAL NOT NULL DEFAULT 0,
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
    msg_id      TEXT NOT NULL,
    line_no     INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    paid        INTEGER NOT NULL DEFAULT 0,
    paid_at     TEXT,
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
    resolved_at     TEXT
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
        # Investors (GEX.PR preferred shareholders): display name + preferred-share count
        # from the Crimson Banking cap-table export, share_pct derived from it, and a
        # running total of profit-share coins paid out.
        "ALTER TABLE investors ADD COLUMN name TEXT",
        "ALTER TABLE investors ADD COLUMN pref_shares REAL NOT NULL DEFAULT 0",
        "ALTER TABLE investors ADD COLUMN share_pct REAL NOT NULL DEFAULT 0",
        "ALTER TABLE investors ADD COLUMN total_received REAL NOT NULL DEFAULT 0",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
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


def adjust_balance(user_id: str, delta: int, *, counts_as_principal: bool = True,
                   reduce_principal: bool = True) -> tuple[int, int, int]:
    """Atomically apply an integer coin delta in a single transaction (no
    read-modify-write race between concurrent coin operations).

    delta > 0 adds coins (and grows principal iff counts_as_principal).
    delta < 0 deducts, clamped at 0 (and reduces principal by the amount actually
    removed iff reduce_principal).

    Returns (coins_after, principal_after, applied_delta) where applied_delta is the
    real change to coins (may be smaller in magnitude than `delta` when clamped)."""
    uid = str(user_id)
    d = int(delta or 0)
    with db() as conn:
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
    return coins, principal, coins - old_coins


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
        for tbl in ("market_stock", "stock_alarms", "market_shares"):
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


def add_investor_payout(user_id: str, amount: float, note: str = None):
    with db() as conn:
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
    """True if a distribution with this note tag already ran — makes the monthly V Tech
    profit share idempotent per (market, month) even if a CSN month is re-ingested."""
    with db() as conn:
        return conn.execute("SELECT 1 FROM investor_payout_log WHERE note=? LIMIT 1",
                            (str(note),)).fetchone() is not None


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
    items: {item: {sold_qty, bought_qty, net_coins}}}}} for one market."""
    mid = market_id or "main"
    with db() as conn:
        mrows = conn.execute(
            "SELECT * FROM csn_history WHERE market_id=? ORDER BY month", (mid,)).fetchall()
        irows = conn.execute(
            "SELECT * FROM csn_history_items WHERE market_id=?", (mid,)).fetchall()
    items_by_month: dict = {}
    for r in irows:
        items_by_month.setdefault(r["month"], {})[r["item"]] = {
            "sold_qty":   int(r["sold_qty"] or 0),
            "bought_qty": int(r["bought_qty"] or 0),
            "net_coins":  float(r["net_coins"] or 0),
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
                    "INSERT INTO csn_history_items (market_id, month, item, sold_qty, bought_qty, net_coins)"
                    " VALUES (?,?,?,?,?,?)",
                    (mid, mk, item, int(iv.get("sold_qty", 0) or 0),
                     int(iv.get("bought_qty", 0) or 0), float(iv.get("net_coins", 0) or 0)),
                )


def csn_all_market_ids() -> list:
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT market_id FROM csn_history").fetchall()]



def get_config(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM bot_config WHERE key=?", (str(key),)).fetchone()
        return row["value"] if row and row["value"] is not None else default


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


def update_bond(bond_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE bonds SET {cols} WHERE id=?",
                     (*fields.values(), int(bond_id)))


def adjust_bond_holding(bond_id: int, user_id: str, d_units: float, d_invested: float,
                        name: str = None) -> None:
    with db() as conn:
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
    """All bot_config rows whose key starts with prefix → {key: value}."""
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


def adjust_etf_units(user_id: str, delta_units: float, delta_cost: float) -> float:
    """Apply +/- units & cost to a holder; clamps tiny/negative remainders to 0.
    Returns the new unit total."""
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO etf_holdings (user_id, units, cost_basis, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET units=units+excluded.units, "
            "cost_basis=cost_basis+excluded.cost_basis, updated_at=excluded.updated_at",
            (str(user_id), float(delta_units), float(delta_cost), now))
        row = conn.execute("SELECT units, cost_basis FROM etf_holdings WHERE user_id=?",
                           (str(user_id),)).fetchone()
        u = float(row["units"]) if row else 0.0
        if u <= 0.0000001:
            conn.execute("UPDATE etf_holdings SET units=0, cost_basis=0 WHERE user_id=?", (str(user_id),))
            u = 0.0
        return u


def upsert_market_stock(market_id: str, item: str, owner: str = None, stock: int = 0,
                        buy_price: float = None, sell_price: float = None,
                        capacity: int = None, buy_qty: int = None,
                        sell_qty: int = None) -> None:
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


def add_loyalty_points(user_id: str, points: float, *, update_activity: bool = True) -> float:
    """Add points to a user. Returns new point total.
    total_earned only ever GROWS: negative deltas (redemption deductions) reduce the balance
    but must not shrink the all-time-earned stat shown in /loyalty stats."""
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
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
                              *, update_activity: bool = True) -> float:
    """Add points to a user's ledger for ONE market — each market owner's own reward
    currency, independent of every other market and of the shared V Tech pool (the
    `loyalty` table). Returns the new point total for that (user, market) pair."""
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
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
                          enchants: str = "", raw_line: str = "", item_key: str = None) -> int:
    """item_key: when the line was picked from the catalog (web builder), link it immediately
    so consignment pricing/CSN matching doesn't need a manual /futures price item match."""
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO futures_bulk_lines (bulk_id, item, qty, unit, enchants, raw_line, item_key) "
            "VALUES (?,?,?,?,?,?,?)",
            (int(bulk_id), str(item), int(qty), str(unit or "pieces"),
             str(enchants or ""), str(raw_line or ""), (str(item_key) if item_key else None)))
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
                     unit_value: float, msg_id: str, line_no: int):
    """Record one parsed harvest line. Returns the new row id if it was NEW, else None
    (idempotent per message+line, so re-ingesting the same Discord message never
    double-counts). The id lets auto-payout settle exactly the rows it just created."""
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO hive_harvests "
            "(market_id, ign, user_id, item, qty, unit_value, msg_id, line_no) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(market_id), str(ign), (str(user_id) if user_id else None), str(item),
             int(qty), float(unit_value or 0), str(msg_id), int(line_no)))
        return int(cur.lastrowid) if cur.rowcount > 0 else None


def get_hive_harvests_by_ids(ids: list) -> list:
    if not ids:
        return []
    with db() as conn:
        q = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT * FROM hive_harvests WHERE id IN ({q}) ORDER BY id",
                            [int(i) for i in ids]).fetchall()
        return [dict(r) for r in rows]


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


def mark_hive_harvests_paid(ids: list) -> int:
    if not ids:
        return 0
    with db() as conn:
        q = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE hive_harvests SET paid=1, paid_at=datetime('now') "
            f"WHERE id IN ({q}) AND paid=0", [int(i) for i in ids])
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


def add_land_entry(land: str, entry_no: int, ts: str, kind: str,
                   amount: float, new_balance, body: str) -> bool:
    """Store one land-inbox entry. Returns True if it was NEW (dedup by PK)."""
    with db() as conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO land_ledger (land, entry_no, ts, kind, amount, new_balance, body)
            VALUES (?,?,?,?,?,?,?)
        """, (str(land), int(entry_no), str(ts), str(kind), float(amount),
              None if new_balance is None else float(new_balance), str(body or "")[:300]))
        return cur.rowcount > 0


def get_land_entries(land: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM land_ledger WHERE land=? ORDER BY entry_no",
                            (str(land),)).fetchall()
        return [dict(r) for r in rows]


def set_land_balance(land: str, balance: float) -> None:
    with db() as conn:
        conn.execute("""
            INSERT INTO land_balances (land, balance, updated_at) VALUES (?,?,datetime('now'))
            ON CONFLICT(land) DO UPDATE SET balance=excluded.balance, updated_at=datetime('now')
        """, (str(land), float(balance)))


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
    last_priced_at, last_priced_month.
    """
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
            "treasury": float(field("treasury_coins", 0.0)),
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
                treasury_coins=excluded.treasury_coins,
                dividend_pct=excluded.dividend_pct,
                last_dividend_month=excluded.last_dividend_month
        """, values)
        row = conn.execute("SELECT * FROM market_shares WHERE market_id=?", (market_id,)).fetchone()
        return dict(row)


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


def adjust_holding(user_id: str, market_id: str, delta_shares: float, delta_cost_basis: float):
    """Apply a buy (+shares/+cost) or sell (-shares/-cost) to a user's holding,
    creating the row if needed. Caller is responsible for checking that a sell
    doesn't take shares negative."""
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("""
            INSERT INTO stock_holdings (user_id, market_id, shares, cost_basis, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, market_id) DO UPDATE SET
                shares=shares + excluded.shares,
                cost_basis=cost_basis + excluded.cost_basis,
                updated_at=excluded.updated_at
        """, (str(user_id), market_id, delta_shares, delta_cost_basis, now))


def log_stock_trade(user_id: str, market_id: str, side: str, shares: float,
                     price_per_share: float, total_coins: float):
    with db() as conn:
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


def adjust_treasury(market_id: str, delta: float, allow_negative: bool = True) -> float:
    """Add (delta>0, e.g. a buy paying in) or remove (delta<0, e.g. funding a
    sell) coins from a market's treasury. When allow_negative is False the
    treasury is only drawn down to zero and the actually-applied delta is
    returned, so the caller can detect (and mint) any shortfall."""
    with db() as conn:
        row = conn.execute(
            "SELECT treasury_coins FROM market_shares WHERE market_id=?", (market_id,)
        ).fetchone()
        if not row:
            return 0.0
        cur = float(row["treasury_coins"] or 0.0)
        applied = float(delta)
        if not allow_negative and (cur + applied) < 0:
            applied = -cur
        conn.execute(
            "UPDATE market_shares SET treasury_coins=? WHERE market_id=?", (cur + applied, market_id)
        )
        return applied



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


def mark_limit_order_filled(order_id: int, fill_price: float, fill_total: float) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE stock_limit_orders SET status='filled', fill_price=?, fill_total=?, "
            "resolved_at=datetime('now') WHERE id=? AND status='open'",
            (float(fill_price), float(fill_total), int(order_id)),
        )


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


def get_last_dividend(market_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM stock_dividend_log WHERE market_id=? ORDER BY id DESC LIMIT 1",
            (market_id,),
        ).fetchone()
        return dict(row) if row else None
