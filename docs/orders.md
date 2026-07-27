# Orders

## What it is
Production requests ("restock orders") that workers claim and fulfill. Each order belongs to
a market and tracks requested vs produced quantity and a status.

## Commands (`/orders …`)
- `orders` — show open production requests.
- `cancel_order id` — (managers) cancel an order by ID.
- `ping_unclaimed` — (managers) ping workers about unclaimed orders.
- `manager_panel` — open the manager control panel (interactive management).
- Related: `/order` (place an order), and `/inventory restock_deficit market` / the
  Inventory-tab "Generate restock orders" button, which create orders from the **real
  shortfall** (capacity − current stock), not just what sold.

## Data / lifecycle
- `orders` table: `item`, `requested`, `produced`, `status` (open/claimed/partial/
  fulfilled), `claimed_by`, `market_id`, per-piece coin reward, priority, ticket links.
- `order_claims` records who claimed how much. Fulfilling pays the worker + loyalty, and
  can ping the ordering market's owner.

## Gotchas the AI must know
- A claimed order isn't a produced one — `status:claimed` with `produced:0` means someone
  reserved it but hasn't delivered.
- Restock quantities are best driven by capacity shortfall (`restock_deficit`) rather than
  by recent sales alone, so shelves actually refill.
- Worker pay per order roughly tracks the item's coin value × produced qty (+ tier bonus).
