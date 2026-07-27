# The AI itself, admin tools, lands & config

## The AI (you)
- **Access is an allow-list**, separate from Discord roles: `/ai_allow add @user`,
  `/ai_allow remove @user`, `/ai_allow list` (managers). Only allow-listed users can
  @mention you. Never claim there's no permission system, and don't confuse it with the
  Manager role.
- `/ai_audit` — (manager) recent AI tool actions: who ran what.
- You can **see images** users attach — read them directly. Never say you can't.
- Your knowledge of the bot comes from `docs/*.md` (this library). If something is
  documented here, it exists — cite the exact command instead of guessing. If you're not
  sure, say so and offer to check rather than inventing behaviour, prices or surcharges.

## Lands (`/land …`)
Claims tracking: treasuries and teleport-fee income.
- `bind` — link a land to a market; its balance becomes that market's **treasury**.
- `status` — balances, bindings, inferred teleport fees per land.
- `feed_channel` — (manager) lock LANDS FEED ingest to one channel (spoof protection).
- Land balances are fed by the mod's `LANDS-BAL` lines; every CSN run doubles as a lands
  checkpoint.

## Land Exchange / auctions (`/realestate …`)
`sell` — list anything for auction in one command (name, price, drag in photos).
`list` — list a plot fixed-price or timed. `listings` — browse active, soonest-ending first.
Bidding, deal rooms and winner handover are handled by the exchange views.

## Admin / repair (`/admin …`, managers, guarded by confirm)
- `wipe` — destructive wipe. `migrate_stock` — move mis-routed live stock between markets.
- `repair_payouts` — find & repay workers paid 0 by the old price-lookup bug.
  `repair_order` — attach a worker to an orphaned order and pay them. `repair_all`.
- `backfill_team_perf` — recover past fulfillments missing from the team ledger.
- `hive_audit` — **detect hive double counting** (same sale ingested under many messages).
- `dedupe_perflog` — remove duplicate team-performance rows.
- `purge_brews` — clean brew names (strip ads, state tags, durations).
- `value_free_stock` — count 0-coin acquired stock (combs/deposits) as profit at market value.

## Config (`/config …`)
`set_channel` — point a bot channel/category at a channel on THIS server. `set_guild`,
`show` (override vs .env default), `reset`. `/network invite|autopost|post` — SW Trade
Network cross-server order broadcasting.

## Gotchas the AI must know
- Slash-command groups max out at **25 subcommands** — that's why stock/alarm commands live
  under `/inventory` and channel binding is the top-level `/bind_market`, not `/market`.
- Admin repair commands move real coins; always describe what they'll do before suggesting them.
