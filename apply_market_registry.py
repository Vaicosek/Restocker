"""One-shot: register market owners + report-channel bindings (2026-07-27 roster).

Sets owner_id + report_channel_id for every listed market, creates any market that
doesn't exist yet (nauticalmarket, sancta), and leaves display names alone where
already set. Run from the bot folder ON THE BOT MACHINE:  python apply_market_registry.py
Safe to re-run (idempotent). The bot picks the changes up immediately (same DB).
"""
import secrets
import sqlite3

DB = "restocker.db"

# (market_id, display name, report channel id, owner discord id)
REGISTRY = [
    ("falrija",          "Falrija",           "1529551677353627898", "1529820990857678979"),
    ("nether_market",    "Nether market",     "1519690325273219083", "1354143289426575391"),
    ("invictusemporium", "Invictus-emporium", "1521518107632599132", "965756490277330964"),
    ("viridianmarket",   "ViridianMarket",    "1522883957832548382", "98468157852778496"),
    ("generalstore",     "GeneralStore",      "1529394249353920542", "1325526839661170809"),
    ("goblin_mart",      "Goblin Mart",       "1529503569584197772", "1362806160486432778"),
    ("freezone",         "Freezone",          "1529538342558105651", "846469784966135819"),
    ("nauticalmarket",   "NauticalMarket",    "1522336398101975160", "488919485462478880"),
    ("toolshop",         "Toolshop",          "1521790087803830292", "1183543527842525264"),
    ("sancta",           "Sancta",            "1531333510378422353", "1478196512818462861"),
    ("amazonia",         "Amazonia",          "1510384815093059805", "1080404147368628254"),
    ("bnl",              "BNL",               "1510943667597348994", "219181322529144833"),
]


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # typical platform fee for new rows (copy whatever existing markets use; fallback 5)
    row = cur.execute(
        "SELECT platform_fee_pct, COUNT(*) c FROM markets "
        "GROUP BY platform_fee_pct ORDER BY c DESC LIMIT 1").fetchone()
    fee = float(row["platform_fee_pct"]) if row else 5.0

    created, updated = [], []
    for mid, name, chan, owner in REGISTRY:
        ex = cur.execute("SELECT market_id, name FROM markets WHERE market_id=?", (mid,)).fetchone()
        if ex:
            cur.execute(
                "UPDATE markets SET owner_id=?, report_channel_id=?, "
                "name=CASE WHEN name IS NULL OR name='' THEN ? ELSE name END "
                "WHERE market_id=?",
                (owner, chan, name, mid))
            updated.append(mid)
        else:
            code = secrets.token_hex(4).upper()
            cur.execute(
                "INSERT INTO markets (market_id, name, owner_id, manager_ids, platform_fee_pct, "
                "csn_history_file, active, discord_role_name, leader_discord_id, leader_code, "
                "report_channel_id) VALUES (?,?,?,?,?,NULL,1,'',?,?,?)",
                (mid, name, owner, "[]", fee, owner, code, chan))
            created.append(f"{mid} (code {code})")
    conn.commit()

    print(f"Updated {len(updated)}: {', '.join(updated)}")
    print(f"Created {len(created)}: {', '.join(created) or '—'}")
    print("\nFinal state:")
    for r in cur.execute(
            "SELECT market_id, name, owner_id, report_channel_id FROM markets "
            "ORDER BY market_id"):
        print(f"  {r['market_id']:<18} {r['name'] or '':<18} owner={r['owner_id'] or '—':<20}"
              f" chan={r['report_channel_id'] or '—'}")
    conn.close()


if __name__ == "__main__":
    main()
