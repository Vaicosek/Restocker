# Pricing doctrine — buyer groups & price levels

## The two buyer groups
- **Inner group (Internal MARKETS)** — trusted partners: **amazonia, bnl, mardurak,
  dragonmart, moosemart, brewshop, greyhames** (people: e.g. Tylics, Law, MrPancake,
  Jacob1304/Amazonia). They buy at **group price** ("Price to group" / "Group Buy Price").
  They may order **futures up to 500k**: pay only **cash cost** up front, then settle the
  remainder up to group price after they resell ("you always get item at cost and pay me
  later").
- **External markets** — everyone else: **falrija, nether-market, invictus-emporium,
  viridianmarket, general_store, goblinmart, freezone, nauticalmarket, toolshop, sancta**.
  They pay the **market sell price** ("Suggested sell price" / "Market Sell Price"). No
  at-cost futures.
- The Discord categories "Internal MARKETS" and "external markets" mirror these groups — a
  market's category tells you which price list applies.

## How a buyer's group is resolved (automatic)
A user's group comes from the markets **registered to them** (owner_id / leader /
manager_ids — set with `/market set_owner` etc.): holding any inner market → **inner**;
only external markets → **external**; no market at all → treat as external unless a
manager says otherwise. A market's group can be overridden with the config key
`market_group:<market_id>` = `inner`|`external`. The `quote_futures` AI tool
accept the buyer (`for_user` / `customer`) and state the applicable price — pass the buyer
whenever known. **Keep owners registered** (`/market set_owner`) or resolution falls back
to "no market → external".

## The three price levels (low → high)
1. **Cash cost** — production cost (materials/diamonds + XP + worker pay). What inner-group
   futures are invoiced at up front.
2. **Inner group price** — what inner groups pay in total (futures settle up to this).
3. **External market price** — retail, what outside buyers/markets pay.

Company margin on an inner deal = group − cash cost. On an external sale = sell − cash cost.

## Gear sheet (per piece) — see also futures.md
| Tier | Cash cost | Inner group | External |
|---|---|---|---|
| P/A/S Eff V + Fortune III/Silk | 2,250 | 2,550 | 2,950 |
| P/A/S Eff V clean | 1,700 | 2,150 | 2,550 |
| Pickaxe Eff IV + Fortune/Silk | 1,400 | 1,950 | 2,350 |
| P/A/S Eff IV clean | 1,100 | 1,450 | 1,850 |
| Sword Sharp V + FA II/KB III | 4,350 | 4,900 | 5,200 |
| Sword Sharp V clean | 2,300 | 3,200 | 3,600 |
| Armor piece | 1,175 | 950 | 1,125 |

## Brew sheet (per piece)
| Brew | Mat cost | Worker | Inner group | External |
|---|---|---|---|---|
| Blood Of Mardurak (Fire Res + Regen) | 102.34 | 71.75 | 164 | 205 |
| Fres Regen / Ussviksye Tyahiliks | 102.34 | 71.75 | 164 | 205 |
| The Hora (Str 2 + Speed 2 + Slow) | 55.16 | 50 | 76 | 95 |
| Invis / Insomniac Mayri | 51.17 | 50 | 76 | 95 |
| Mardurak-Haste (Haste 5) | 51.17 | 50 | 101.88 | 127.35 |
| Emporium-Warlord (Str 2 + Speed 2) | 45 | 50 | 76 | 95 |
| Speed2 (Speed 2) | 22.50 | 50 | 96 | 120 |
| Obidios Nuclear Power (XP Brew) | 1,657.03 | 297.50 | 680 | 850 |
| Mardurak Redstone Enhancer (Haste5+Speed2) | 83.83 | 52.50 | 120 | 150 |
| Cell's Regeneration (Regen 1) | 90 | 50 | 101.88 | 127.35 |
| Honey Comb 2 (HBoost1+Regen1) | 186.17 | 66.50 | 152 | 190 |
| Thick Skin (HP Boost I) | 102.34 | 50 | 101.88 | 127.35 |
| Greyhame Dragon Scales (Fire Res) | 26.56 | 50 | 101.88 | 127.35 |

Brew cash cost = material + worker cost. Note some brews sell BELOW cost (e.g. Obidios XP
brew: cost ~1,954 vs sell 850) — that's known and intentional; don't "correct" it.

## Gotchas the AI must know
- When someone asks "how much for X", ask (or infer from who's asking) whether they're
  inner group or external — the answer differs.
- Futures at cash cost are an inner-group privilege (limit 500k); externals pay sell price.
- Armor is priced below cash cost for the group (950 < 1,175) — intentional, a perk.
