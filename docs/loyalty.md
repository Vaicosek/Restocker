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

## Commands
- `/loyalty stats` — your points, tier, next tier. `/loyalty leaderboard` — top holders.
- `/loyalty redeem` — spend points on a reward (a manager/owner pays it out).
  `/loyalty redemptions` / `approve` / `deny` — the approval queue.
- `/loyalty set_points` / `add_points` — (manager) adjust points.

## IGN registry (critical for pay)
- `/register_ign` — a worker links their Minecraft name. **Run again to add alts.**
- `/link` / `/unlink` — (manager) link or remove a member's IGN(s).
- `/unlinked` — (manager) employees with no IGN. `/remind_unlinked` — DM them all.

## Gotchas the AI must know
- **Pay routes through the IGN registry.** Hive wages and order payouts resolve the
  in-game name → Discord account. An unregistered IGN's money is **held, not lost**, and
  pays automatically once they register.
- Anti-squatting: an IGN that already has unpaid value attached can't be self-claimed — a
  manager must link it.
- Loyalty points come from the VALUE of work, and tier bonuses apply to future payouts only.
