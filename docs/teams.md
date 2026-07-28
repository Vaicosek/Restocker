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

## Name aliases
The CSN mod learns potion and tool names from captured item lore automatically — the old
`/brew` and `/tool` alias commands were retired.

## Gotchas the AI must know
- A worker's team IGN must match their in-game name **exactly** or their sales won't
  attribute to the team.
- Team performance can be inflated by re-logged orders — ask the AI to check for duplicate
  performance rows if a total looks too high.
