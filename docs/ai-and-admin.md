# The AI itself, admin tools, lands & config

## The AI (you)
- **Access is an allow-list**, separate from Discord roles: `/ai_allow add @user`,
  `/ai_allow remove @user`, `/ai_allow list` (managers). Only allow-listed users can
  @mention you. Never claim there's no permission system, and don't confuse it with the
  Manager role.
- `/admin ai_audit` — (manager) recent AI tool actions: who ran what.
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
`list` — list a plot fixed-price or timed. `listings` — browse active, soonest-ending first.
`bid` / `buy` / `cancel` / `close` / `info` — bidding and handover. `config` / `notify_role` /
`notifypanel` — where auction alerts go.
Bidding, deal rooms and winner handover are handled by the exchange views.

## Admin / repair (`/admin …`, managers, guarded by confirm)
- `wipe` — destructive wipe (confirm-guarded).
- `ai_audit` — recent AI tool actions: who ran what.
- `dm_setup` — DM every market owner their market id, CSN code and webhook, with setup
  instructions (also available to the AI as a tool).
- `rebuild_market_channel` — delete a market channel's messages and repost one clean
  earnings summary per month.
- `fix_month_close` — correct or delete stale month-closing posts.
- `csn_cleanup` — delete raw CSN upload/noise messages from a channel.
- Everything else (payout repair, stock migration, hive double-count audits, backfills) is
  now AI-side — ask the bot rather than looking for a command.

## Config (`/config …`)
`set_channel` — point a bot channel/category at a channel on THIS server. `set_guild`,
`show` (override vs .env default), `reset`.

## Gotchas the AI must know
- Slash-command groups max out at **25 subcommands** — that's why stock/alarm commands live
  under `/inventory` and channel binding is the top-level `/bind_market`, not `/market`.
- Admin repair commands move real coins; always describe what they'll do before suggesting them.
