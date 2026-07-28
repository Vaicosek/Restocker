# Teams, projects & workers

## Teams (`/team …`)
Workers belong to a manager's team; performance is tracked per team.
- `join` — a worker joins a manager's team and registers their **exact** in-game name.
- `add` / `remove` / `list` / `name` — (manager) roster + team display name.
- `mine` — who your manager is + your registered IGN.
- `csn` — (manager) your team's chest-shop sales for the latest CSN month.
- `leaderboard` — how teams compare (per-team + cross-team).
- `webhook` / `channel` / `unbind` — where the team's performance feed posts.

## Projects (`/project …`)
Hand a manager a budget to build something; they pay their team and keep the rest.
- `create` — fund a manager with a budget. `pay` — (manager) pay a team member from it.
- Hive harvest wages are logged as project work (`project:hive-harvesting`) so the cost of
  harvesting is always visible.

## Enchant areas (`/enchant_area …`)
Roster of which employees operate which enchant-table area: `set` / `list` / `remove` /
`clear`, binding IGNs to an area.

## Name aliases
- `/brew set|remove|list` — map potion codes (`Potion#32L`) to readable names.
- `/tool set|remove|list` — same for tool/equipment codes (`Diamond Pickaxe#ahc`).
  These make CSN reports readable; the mod can also learn brew names from captured lore.

## Gotchas the AI must know
- A worker's team IGN must match their in-game name **exactly** or their sales won't
  attribute to the team.
- Team performance can be inflated by re-logged orders — `/dedupe_perflog` cleans that.
