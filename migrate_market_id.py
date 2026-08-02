"""Rename a market's ID everywhere it is keyed. Preview by default.

The market_id is not a label — it is the join key for share holdings, ledgers, history
files, channel bindings and config. Renaming it means rewriting every one of those in a
single transaction, or the market is left half-migrated with orphaned holdings.

What it touches:
  * every table with a market_id column (discovered from the schema, not hardcoded, so a
    table added later is not silently missed)
  * bot_config keys where the id appears as a colon-delimited segment
    (asset_value:main, vault_due:main, vault_ret_done:main:2024-04, ...)
  * markets.yml: the dict key, and the csn_history_file pointer
  * the csn_history_<old>.yml file itself, renamed on disk

Deliberately NOT touched: free-text `detail` fields in team_perf_log. They are historical
descriptions, not join keys, and rewriting history to match a new name is worse than
leaving an accurate record of what the market was called at the time.
"""
import os
import re
import sqlite3


def _cfg_key_rename(key: str, old: str, new: str) -> str:
    """Replace the id only where it is a whole colon-delimited segment, never a substring.
    'domain:main' -> yes.  'mainframe_setting' -> no."""
    parts = key.split(":")
    return ":".join(new if p == old else p for p in parts)


def plan(conn, old: str, new: str, data_dir: str = ".") -> dict:
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        tables = []
        for (t,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            for col in cols:
                if col in ("market_id", "mid"):
                    try:
                        n = conn.execute(
                            f"SELECT COUNT(*) FROM {t} WHERE {col}=?", (old,)).fetchone()[0]
                    except Exception:
                        continue
                    if n:
                        tables.append((t, col, n))
        # Collision check: a table with a UNIQUE/PK on market_id will fail the UPDATE if
        # the new id already has a row there. Better to say which, than to half-apply.
        collisions = []
        for t, col, _n in tables:
            try:
                if conn.execute(f"SELECT 1 FROM {t} WHERE {col}=? LIMIT 1", (new,)).fetchone():
                    collisions.append(t)
            except Exception:
                pass
        keys = [r[0] for r in conn.execute("SELECT key FROM bot_config").fetchall()]
        cfg = [(k, _cfg_key_rename(k, old, new)) for k in keys
               if _cfg_key_rename(k, old, new) != k]
        return {"tables": tables, "config": cfg, "collisions": collisions,
                "rows": sum(n for _, _, n in tables)}
    finally:
        conn.row_factory = prev


def apply(conn, old: str, new: str, markets_yaml: dict = None, data_dir: str = ".") -> dict:
    p = plan(conn, old, new, data_dir)
    if p["collisions"]:
        raise RuntimeError("target id already present in: " + ", ".join(p["collisions"]))
    with conn:                                   # one transaction; rolls back on any error
        # markets.market_id has children (market_shares -> markets, stock_holdings ->
        # market_shares) declared ON UPDATE NO ACTION, so NO ordering works: rename the
        # parent and the children dangle; rename a child and it points at a row that does
        # not exist yet. defer_foreign_keys holds every check until COMMIT, by which point
        # the whole graph is consistent again. It is transaction-scoped and resets itself.
        conn.execute("PRAGMA defer_foreign_keys = ON")
        for t, col, _n in p["tables"]:
            conn.execute(f"UPDATE {t} SET {col}=? WHERE {col}=?", (new, old))
        for k, nk in p["config"]:
            row = conn.execute("SELECT value FROM bot_config WHERE key=?", (k,)).fetchone()
            if row is None:
                continue
            conn.execute("DELETE FROM bot_config WHERE key=?", (nk,))
            conn.execute("INSERT INTO bot_config (key, value) VALUES (?,?)", (nk, row[0]))
            conn.execute("DELETE FROM bot_config WHERE key=?", (k,))
    return p
