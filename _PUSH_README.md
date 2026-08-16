# What is in this archive

Extract over `C:\Users\Vaicos\Desktop\AI\RestockerLocal`, keeping folders.
Nothing here deletes anything; it overwrites 6 files and adds 35.

## MODIFIED (6)
Restocker_main.py · Restocker_db.py · Restocker_web.py · bank_api.py
cogs/land_exchange.py · cogs/loops.py

## NEW — money core
ledger_v2.py · ledger_migrate.py · land_escrow.py · land_settle.py
action_log.py · panel_skus.py · csn_sig.py · split_rules.py · ign_links.py

## NEW — website (the hub)
vt_web_shell.py · hub_web.py · banking_web.py · estates_web.py
messages_web.py · reconcile_loop.py

## NEW — cogs
cogs/rollback.py · cogs/panel_skus.py · cogs/splits.py

## NEW — tests (17)
test_website.py · test_hub_web.py · test_messages_web.py
test_csn_ingest.py · test_csn_migration.py · test_csn_health_tool.py
tests/*.py

---

# Before you commit

1. **Branch first.** GitHub Desktop is on `main`.
   Branch > New Branch > `feat/hub-and-ledger` > publish > commit there > open a PR.
   Never straight to main — that repo is public and a Wisp server auto-pulls it.

2. **`.env` is not in here and must never be committed.** Check the changes list before
   you hit commit; if `.env` appears, do not commit.

3. **The database is separate.** The share transfer is in the .db file I sent, not in
   this archive. Stop the bot, back up restocker.db, swap it, start the bot.

# On boot you should see

     Hub section registered
     Banking section registered
     Estates section registered
     Messages section registered
     History section not registered: ModuleNotFoundError ...

That last line is expected — history_web.py is still being built. Everything else
carries on; each section registers independently so one failure cannot take the site down.

# Not done yet, and known

- `history_web.py` — being built; adds per-user transaction history
- Read-only staff "view as" — not built
- The 7 credentials in the .env you uploaded still need rotating. Only you can do that.
