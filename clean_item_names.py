"""One-off cleanup for scraped item names. Run with --apply to write; default is a preview.

Two DIFFERENT jobs, because the two kinds of damage are not the same:

1. LORE-CONTAMINATED GEAR (e.g. the pickaxe that swallowed a server announcement).
   The real enchants are in there; everything else is noise. Salvage to base + enchants.
   Only applied when the result still has at least one enchant AND is not already taken.

2. POTIONS / BREWS — NOT TOUCHED. Their names are entirely flavour text BY DESIGN; the
   lore is what distinguishes one brew from another. Stripping it collapses every potion
   to the bare string "Potion" — 28 rows here, holding 378/354/312/161/61/57 units of
   real stock, would merge into one. Pass --brews to remove colour codes (words kept,
   rows stay distinct) if you only want them tidier.

Safety: refuses to rename onto an existing name (that IS the merge we're avoiding), and
does nothing at all without --apply.
"""
import re
import sqlite3
import sys

# Vanilla codes are 0-9a-f k-o r, but the Brewery plugin adds its own (§p distilled,
# §s stars, §t lore, §u, §y barrel-aged). Matching only the vanilla set left those in
# the "cleaned" name. Strip §<any alphanumeric>, plus the §x§R§R§G§G§B§B hex form.
SECT = re.compile(r'§(?:x(?:§[0-9a-zA-Z]){6}|[0-9a-zA-Z])')
ENCH = re.compile(r'^(?:[A-Z][a-z]+(?: [A-Z][a-z]+)*) (?:[IVX]+|\d+)$')
JUNK = re.compile(r'»|«|§')


def strip_codes(s: str) -> str:
    """Remove Minecraft colour/format codes, leaving the words."""
    return re.sub(r'\s{2,}', ' ', SECT.sub('', s)).replace(' ,', ',').strip()


def salvage_gear(name: str):
    """base + only the enchant-shaped tokens. None if nothing survives."""
    base, sep, tail = name.partition(' - ')
    if not sep:
        return None
    keep = []
    for chunk in strip_codes(tail).split(','):
        t = chunk.strip().strip('.').strip()
        if ENCH.match(t) and t not in keep:
            keep.append(t)
    if not keep:
        return None
    return f"{base.strip()} - " + ", ".join(keep)


def plan(conn, brews: bool = False):
    # Do not depend on the caller's row_factory: with the default (plain tuples) the
    # dict(r) below raises ValueError, and the AI tool passes the bot's own connection.
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return _plan(conn, brews)
    finally:
        conn.row_factory = prev


def _plan(conn, brews: bool = False):
    out = []
    for table, col, keycols in (("items", "name", ("name",)),
                                ("market_stock", "item", ("market_id", "item"))):
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
        existing = {r[col] for r in rows}
        # Also track names THIS RUN would create. Two rows whose only difference was a
        # colour code (e.g. two Invisibility potions differing by §x hex) clean to the
        # SAME string — a collision invisible to a check against existing names alone.
        planned = {}
        for r in rows:
            old = r[col]
            if not isinstance(old, str) or not JUNK.search(old):
                continue
            if old.lower().startswith("potion"):
                # Brews are SUPPOSED to look like this: the lore IS the product name,
                # and it is the only thing telling two potions apart. Left alone by
                # default. --brews de-colours them (words kept) if you want them tidier.
                if not brews:
                    continue
                new = strip_codes(old)          # de-colour only; never merge
                kind = "potion (de-colour)"
            else:
                new = salvage_gear(old)
                kind = "gear (salvage enchants)"
                if not new:
                    out.append((table, col, r, old, None, "SKIP — nothing salvageable"))
                    continue
            if new == old:
                continue
            if new in existing and new != old:
                out.append((table, col, r, old, new, "SKIP — target name already exists (would merge)"))
                continue
            key = (r.get("market_id"), new) if table == "market_stock" else new
            if key in planned:
                out.append((table, col, r, old, new,
                            "SKIP — collides with another cleaned row (differed only by colour code)"))
                continue
            planned[key] = old
            out.append((table, col, r, old, new, kind))
    return out


def main():
    apply = "--apply" in sys.argv
    db = next((a for a in sys.argv[1:] if not a.startswith("-")), "restocker.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    items = plan(conn, brews="--brews" in sys.argv)
    changes = [x for x in items if x[4] and not x[5].startswith("SKIP")]
    skips = [x for x in items if x[5].startswith("SKIP")]

    for table, col, r, old, new, kind in items:
        tag = "SKIP" if kind.startswith("SKIP") else "CHANGE"
        print(f"\n[{tag}] {table}.{col}  ({kind})")
        print(f"  FROM {old[:150]!r}")
        print(f"  TO   {new!r}" if new else "  TO   —")

    print(f"\n{len(changes)} change(s), {len(skips)} skipped, db={db}")
    if not apply:
        print("PREVIEW ONLY — re-run with --apply to write.")
        return
    for table, col, r, old, new, kind in changes:
        if table == "items":
            conn.execute("UPDATE items SET name=? WHERE name=?", (new, old))
        else:
            conn.execute("UPDATE market_stock SET item=? WHERE market_id=? AND item=?",
                         (new, r["market_id"], old))
    conn.commit()
    print(f"APPLIED {len(changes)} change(s).")


if __name__ == "__main__":
    main()
