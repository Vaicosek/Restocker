# Futures orders & production pricing

## What it is
A **futures order** is a custom build request from a customer (e.g. "5× Eff V Fortune III
pickaxe"). It's reviewed, approved, then produced and delivered.

## Pricing — THE RULE
**Futures are quoted at `cash_cost`** — the production cost (diamonds + XP value + worker
pay). There IS a fixed cost sheet; pricing is NOT ad-hoc "set by the manager at approval".
Ask the AI to quote it (`quote_futures` tool) — never estimate.

**Settlement model:** the customer pays **cash cost up front**, then after they resell the
goods they pay the remainder up to **price to group** — i.e. total paid = group price, and
the after-sale balance = group total − what they already paid. (Example: 20× Eff IV
ench @1,950 + 5× Eff V ench @2,550 = 51,750 group total; paid 44,250 up front → 7,500 due
after sale.) The company's margin = group total − cash-cost total.

**Unbreaking III is included in every tier. There is no "Unb III surcharge."**

### Production cost sheet (per piece)
| Tier | Diamonds | XP | Worker pay | **Cash cost** | To group | Sell |
|---|---|---|---|---|---|---|
| Pickaxe/Axe/Shovel — Eff V + Fortune III/Silk | 750 | 1,170 | 1,500 | **2,250** | 2,550 | 2,950 |
| Pickaxe/Axe/Shovel — Eff V, clean | 500 | 780 | 1,200 | **1,700** | 2,150 | 2,550 |
| Pickaxe — Eff IV + Fortune III/Silk | 400 | 585 | 1,000 | **1,400** | 1,950 | 2,350 |
| Pickaxe/Axe/Shovel — Eff IV, clean | 250 | 390 | 850 | **1,100** | 1,450 | 1,850 |
| Sword — Sharp V + Fire Aspect II/Knockback III | 750 | 1,170 | 3,600 | **4,350** | 4,900 | 5,200 |
| Sword — Sharp V, clean | 500 | 780 | 1,800 | **2,300** | 3,200 | 3,600 |
| Armor piece | 500 | 780 | 675 | **1,175** | 950 | 1,125 |

Tier selection (`_futures_tier`): sword → sword tier; helmet/chestplate/leggings/boots →
armor; pickaxe/axe/shovel → tool. "Enchanted" = has Fortune / Silk Touch / Fire Aspect /
Knockback. Eff V vs Eff IV picks the tier band.

## Commands
- `/futures_order` — a customer files a request; posts a review card to the futures channel
  pinging **@Owner**.
- `/futures_bulk` — file several lines at once as one bulk request.
- **Quoting is AI-side**: ask the bot and it runs the `quote_futures` tool against the sheet
  above (cash cost + group + sell + breakdown). There is no `/futures_quote` command.
- Listing/billing commands were retired — ask the AI for order status instead.

## Approval → fulfillment flow
1. Order posts to the futures channel with **Approve & Ping Workers / Approve (no ping) /
   Decline** (buttons require manager access).
2. Approving **creates a real claimable work order** in the worker channel — it then behaves
   like any restock order (claim → produce → fulfilled → verified → paid). "Approve (no
   ping)" only skips the @Employee ping; the order still exists.
3. **Self-approval is blocked**: you can't approve your own futures order, and a manager who
   claimed/fulfilled an order can't sign off their own work — another manager must.

## Gotchas the AI must know
- Never invent surcharges or say "price is set case-by-case" — the sheet above is
  authoritative and the `quote_futures` tool computes it.
- Catalog `coin` is the **retail shop price**, not the futures price. Don't quote futures
  from the catalog.
