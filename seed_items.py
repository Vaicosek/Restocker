"""One-time catalog seed, called from main.py on startup. Each group is guarded by
its own flag in bot_config, so it runs ONCE and restarts won't re-run it or reset
stock. Bump a group's flag (e.g. _seeded_gear_v2) to re-apply after an edit.
Also safe to run by hand:  python seed_items.py

NOTE on prices: gear uses the SUGGESTED SELL price; brews use the WORKER PAY figure
(the only number provided). `coin` is one field, so adjust later with /set_price if
you want them on the same basis."""
import Restocker_db as db

MARKET = "main"

# Crafted gear - (name, suggested sell price)
GEAR = [
    ("Pickaxe/Axe/Shovel - Eff V + Fortune III/Silk Touch", 2950),
    ("Pickaxe/Axe/Shovel - Eff V (clean)", 2550),
    ("Armor piece", 1525),
    ("Sword - Sharp V (clean)", 3600),
    ("Sword - Sharp V + Fire Aspect II/Knockback III", 5600),
    ("Pickaxe/Axe/Shovel - Eff IV (clean)", 1850),
    ("Pickaxe - Eff IV + Fortune III/Silk Touch", 2350),
]

# Brews - (name, worker pay) [rounded to whole coins]
BREWS = [
    ("Blood Of Mardurak (Fire Res + Regen)", 72),
    ("The Hora (Strength 2 + Speed 2 + Slow)", 50),
    ("Fres Regen / Ussviksye Tyahiliks", 72),
    ("Invis / Insomniac Mayri", 50),
    ("Mardurak-Haste (Haste 5)", 50),
    ("Emporium-Warlord (Str 2 + Speed 2)", 50),
    ("Speed2 (Speed 2)", 50),
    ("Obidios Nuclear Power (XP Brew)", 298),
    ("Mardurak Redstone Enhancer (Haste5+Speed2)", 53),
    ("Cell's Regeneration (Regen 1)", 50),
    ("Honey Comb 2 (HBoost1+Regen1)", 67),
    ("Thick Skin (HP Boost I)", 50),
    ("Greyhame Dragon Scales (Fire Res)", 50),
]


def _seed_group(flag, items, label):
    if db.get_config(flag):
        return 0
    for name, price in items:
        db.upsert_item(name=name, coin=int(price), stock=0,
                       unit_type="pieces", stackable=False, stack_size=1,
                       barrel_slots=54, market_id=MARKET)
    db.set_config(flag, "1")
    print(f"[seed] added {len(items)} {label} to market '{MARKET}'")
    return len(items)


def main():
    try:
        db.init_db()
        _seed_group("_seeded_gear_v1", GEAR, "gear item(s)")
        _seed_group("_seeded_brews_v1", BREWS, "brew item(s)")
    except Exception as e:
        print(f"[seed] failed: {e}")


if __name__ == "__main__":
    main()
