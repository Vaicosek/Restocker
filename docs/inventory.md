# Inventory, stock scans & restocking

## What it is
Live barrel stock per market, captured by the CSN mod's **stock scan** (a second keybind:
toggle ON, click each shop, toggle OFF). Drives fullness bars, low-stock alerts and
shortfall-based restock orders.

## Commands
- `/inventory restock_deficit market` — (manager) create restock orders from the **real
  shortfall** (capacity − current stock), not just what sold.
- `/inventory set_capacity market item cap` — define an item's "full".
- `/inventory set_alarm|alarms|clear_alarm` — owner stock alarms (per item or `*` default,
  by pct or pieces). On import the owner is DM'd what's past the alarm, with a one-click
  "Create restock orders" button.
- `/item_edit item [coin] [stackable] [stack_size]` — (manager) fix an item's price or
  stackability. `/item_info` shows price, stock, barrel size and stackability.

## Capacity & fullness
- A barrel = **54 slots × stack size**. Non-stackable items (tools, armor, potions) are
  stack size 1 → a barrel is 54 pieces, not 3,456.
- Capacity defaults to the high-water mark seen in scans; `set_capacity` overrides it.
- Fullness = stock ÷ capacity. ≤20% is "low" (red).

## Website
The **Inventory** tab shows per-market fullness; **My Market** adds a monthly report,
fullness summary, and a **Restock next** list (lowest-fullness items with the shortfall,
click an item to prefill the restock forms).

## Gotchas the AI must know
- If an item is wrongly marked stackable, its barrel size (and therefore fullness and
  restock quantity) is 64× off — fix with `/item_edit … stackable:False`.
- A market with no stock scan shows 0% everywhere; that means "never scanned", not "empty".
