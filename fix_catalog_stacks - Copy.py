"""One-time catalog repair: fix items auto-registered with stackable=0/stack_size=1.

CSN auto-registration saved many items (Azalea Leaves, Cherry Log, Cooked Beef, ...)
as non-stackable, which breaks barrel-capacity math (54 instead of 3,456) and
per-barrel prices. This re-derives each item's stack size from the bot's own
name-detection rules and updates the items table.

Run with the BOT STOPPED, from the bot folder:
    .venv\\Scripts\\python.exe fix_catalog_stacks.py
It prints every change before committing. Backs up nothing — but only touches
the stackable/stack_size columns, and re-running is always safe (idempotent).
"""
import sqlite3

# ── mirror of Restocker_main's detection rules ───────────────────────────────
_NON_STACKABLE_KEYWORDS = {
    "pickaxe", "axe", "shovel", "hoe", "fishing rod", "flint and steel", "shears", "spyglass",
    "sword", "bow", "crossbow", "trident",
    "helmet", "chestplate", "leggings", "boots", "elytra", "shield", "horse armor",
    "shulker box", "saddle", "totem", "goat horn",
    "potion of", "splash potion", "lingering potion",
}
_BREW_EFFECT_WORDS = {
    "haste", "speed", "strength", "weakness", "slowness", "blindness", "poison",
    "regeneration", "regen", "absorption", "fire resistance", "fres", "night vision",
    "invisibility", "invis", "luck", "unluck", "levitation", "levi", "jump boost",
    "mining fatigue", "nausea", "wither", "turtle master", "slow falling", "resistance",
    "instant health", "instant damage", "saturation", "hp boost", "hp2", "hp1",
    "extended", "splash", "drinkable", "splashable",
}
_STACK_16_KEYWORDS = {
    "ender pearl", "snowball", "egg", "empty bucket", "sign", "banner",
    "honey bottle", "boat", "minecart",
}


def detect_stack_size(item_name: str) -> int:
    name_lower = item_name.lower().strip()
    if ":" in name_lower:
        return 1
    for word in _BREW_EFFECT_WORDS:
        if word in name_lower:
            return 1
    for kw in _NON_STACKABLE_KEYWORDS:
        if kw in name_lower:
            return 1
    for kw in _STACK_16_KEYWORDS:
        if kw in name_lower:
            return 16
    return 64


def main():
    conn = sqlite3.connect("restocker.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT name, stackable, stack_size FROM items").fetchall()
    fixes = []
    for r in rows:
        want = detect_stack_size(r["name"])
        have = 1 if not r["stackable"] else int(r["stack_size"] or 64)
        if have != want:
            fixes.append((r["name"], have, want))
    if not fixes:
        print("Catalog already correct — nothing to fix.")
        return
    print(f"Fixing {len(fixes)} item(s):")
    for name, have, want in fixes:
        print(f"  {name}: stack {have} -> {want}")
        conn.execute("UPDATE items SET stackable=?, stack_size=? WHERE name=?",
                     (0 if want == 1 else 1, want, name))
    conn.commit()
    print(f"\nDone — {len(fixes)} item(s) corrected. "
          f"Start the bot and re-run /csn with your stock CSV to recompute capacities.")


if __name__ == "__main__":
    main()
