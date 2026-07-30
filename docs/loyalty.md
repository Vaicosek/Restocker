# Loyalty, IGNs & rewards

## What it is
Workers earn **loyalty points** for work (orders fulfilled, hive harvests). Points set a
**tier**, which grants an interest rate on savings and a payout bonus. Points can be
redeemed for rewards.

## Tiers
| Tier | Points | Interest/wk | Payout bonus |
|---|---|---|---|
| Recruit | 0 | 0.05% | +0% |
| Worker | 1,000 | 0.1% | +2% |
| Veteran | 5,000 | 0.2% | +5% |
| Expert | 15,000 | 0.35% | +8% |
| Elite | 40,000 | 0.5% | +12% |

## Where it lives (no /loyalty command any more)
- **`/me`** — every worker-facing thing in one panel: coins, in-game names, team, and
  loyalty points/tier. Its **Loyalty & rewards** button opens the hub: leaderboard,
  redeem, and (managers only) the settings panel and the pending-redemption queue.
- Managers reach add/set points, link/unlink IGNs, the unlinked-employee list, IGN
  look-up and the reminder preview from that same hub → **Manager settings**.
- There is no `/loyalty`, `/balance`, `/register_ign` or `/team join` any more — all four
  folded into `/me`.

## IGN registry (critical for pay)
- `/me` → **Link in-game name** — a worker links their Minecraft name. **Run again to
  add alts**; alts pool into one account.
- Money-bearing IGNs cannot be self-claimed: if an IGN has unpaid harvests waiting, a
  manager must link it after verifying it's theirs. Anti-squatting, and it still applies
  through the panel.
- The panel PREVIEWS the unlinked reminder only. Actually DMing — and especially the
  deadline that strips roles — goes through the bot so it can confirm first.

## Gotchas the AI must know
- **Pay routes through the IGN registry.** Hive wages and order payouts resolve the
  in-game name → Discord account. An unregistered IGN's money is **held, not lost**, and
  pays automatically once they register.
- Anti-squatting: an IGN that already has unpaid value attached can't be self-claimed — a
  manager must link it.
- Loyalty points come from the VALUE of work, and tier bonuses apply to future payouts only.
