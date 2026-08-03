# CSN Mod ↔ Bot Sync Audit — 2026-08-03

**Goal:** make the mod and bot act as ONE system — the mod is the bot's hands in-game.
Three agents audited bot-side, mod-side, and the integration contract, running real
harnesses against the actual parsers and byte-faithful reproductions of the mod's CSV
writers. Everything below is verified, not speculated, with file:line on both sides.

**This file is the work order for the sync session. Fix in the order listed.**

---

## PHASE 1 — Protocol unification (everything financial is wrong until done)

### 1. Mod writes DELTAS, bot guesses cumulative → 33–50% of earnings discarded
- Mod: `finish()` → `writeMonthlyReport(targets, runFresh)` (CsnExportClient.java:638) —
  every monthly block is only THIS RUN's fresh entries.
- Bot: `Restocker_main.py:6747` classifies cumulative when `(up+reset)/classified >= 0.8`
  and then `:6763` keeps only the LAST block per segment.
- Measured: 2 runs both rising → 33% lost; 3 monotone runs → 50% lost. Single-item
  markets misclassify ~100% of the time.
- **Fix:** mod stamps `# MODE,delta` in the monthly header; bot honours it (sum blocks)
  instead of classifying. Keep the classifier only for legacy files with no MODE header.

### 2. No shared sale identity → duplicates AND wrongly-dropped legit sales
- Mod dedup: `stableEntryHash` = actor|verb|qty|item|coins|**hour bucket**
  (CsnExportClient.java:1441). Two identical sales 40 min apart hash EQUAL → second sale
  silently never exported (verified).
- Bot dedup: `uq_csn_txn (market_id, actor, item, qty, coins, sale_ts)`
  (Restocker_db.py:371) keys on EXACT reconstructed `sale_ts` = export_instant − N min,
  which drifts up to a minute per run → same sale re-ingested as duplicate (verified;
  see also data/exports/csn_export_2026-05-01.csv — rows share seconds+millis).
- `verb` and `seller` absent from the index: a bought and a sold row at the same instant
  collapse (verified rowcount 0).
- **Fix:** mod emits a per-sale stable id column (UUID or minute-bucket hash); bot adds
  it to the CSV contract and keys `uq_csn_txn` on (market_id, sale_uid). Interim bot-only
  mitigation: index on minute-bucket `substr(sale_ts,1,16)` + verb + seller.

### 3. Gear/brew alias learning from stock scans is DEAD CODE
- Mod strips `#code` BEFORE writing the stock CSV (`:755` regex, `:814/:870` writes
  display name). Bot's `_learn_brew_aliases_from_stock` (Restocker_main.py:3353) requires
  `"#" in raw_item` → learns 0, always (verified). The 15 learned brews came from the
  csn_profiles.json path, not this one.
- Also: sales path strips HEX-ONLY codes (`:549` `#[0-9a-fA-F]{1,6}$`) while stock strips
  alnum codes → sales say `Potion#akQ`, stock says `Potion`; names never match.
- **Fix:** mod adds a `raw_item` column to csn_stock (one `csvField(sh.itemRaw())` at
  CsnExportClient.java:871); bot reads it. Unify the two code-stripping regexes.

### 4. Human-upload path has NO AUTH — anyone can forge earnings
- `cogs/events.py:123-142`: any guild member's attachment named `*csn_*.csv` goes
  straight to `_process_csn_attachment` — no role, no code, no TOFU (that only gates
  webhooks, :207). In a bound channel a forged file books earnings, re-prices shares,
  and (export path) pays harvest wages.
- `txn_only` never passed on this path → human dropping both files runs earnings twice.
- **Fix:** require `_is_mgr(author)` OR a verifying `# MARKET,<id>,<code>` header; pass
  `txn_only` identically to the webhook path.

### 5. Market code is a plaintext bearer token that beats channel binding
- `# MARKET,<id>,<code>` in every CSV; code from any readable channel can be lifted.
- `Restocker_main.py:4012`: valid code OVERRIDES the channel binding (posts into market A
  from anywhere). `:4062-4069`: on an unbound channel a valid code AUTO-BINDS that
  channel → report exfiltration + denial of delivery.
- TOFU global vouch (`events.py:222-254`): one valid file promotes the poster for ALL
  markets forever. First-poster lock-in with no code check when lists are empty (:215).
- **Fix:** bind posters per-market, not globally; code never overrides an existing
  binding; rotate codes out of the file body (HMAC over content) longer-term.

### 6. TWO harvest payout engines, no shared ledger, ~80× apart
- Export path `_pay_honey_from_export` (Restocker_main.py:3664): 64/76 coins per piece
  (`_HARVEST_RATES` :3560).
- Hive path cogs/hive.py: `_hive_item_value` × 17% ≈ 0.93/piece (:2743, :2768).
- No shared ledger (`harvest_last_ts:<mid>` string-compare vs `uq_hive_sale`). Re-upload
  of an export alone pays the whole period AGAIN at the 64/76 rate. `harvest_last_ts` is
  a `>` compare on a DRIFTING reconstructed ts: +30s drift double-pays, −30s drift makes
  newer sales invisible forever.
- `_hive_item_value` does NOT strip `§` (:2755) → `§6Honey Block` values 0, harvester
  paid NOTHING (while `_harvest_rate_for` substring-match DOES match it).
- **Fix:** one ledger keyed on the per-sale uid (see #2); strip `§` in
  `_hive_item_value`; retire `_HARVEST_RATES` or reconcile the two rates.

---

## PHASE 2 — Data-loss bugs (bot)

### 7. Purchase-only exports fully discarded
`Restocker_main.py:3987` `if not items: return` sits BEFORE txn ingest (:4084) and hive
payout (:4192); `_parse_export_csv` only fills `items` on `bought`. A 0-coin collection
shop that only buys is invisible. **Move the guard below txn/hive ingest.**

### 8. Export clobbers the whole current month
`:3978` export month_key = NOW, and `_record_to_market_history:8798` REPLACES
months[month_key]. An export-only upload overwrites correct cumulative totals with one
period's partials. `txn_only` only protects the same-message case. **Derive month from
the transactions' timestamps and MERGE.**

### 9. DB-lock fallback destroys market history
`:8561-8567`: read error → `{"months":{}}` fallback; then save → `csn_save_market`
DELETE-all + reinsert only what was loaded (Restocker_db.py:1541). YAML mirror is stale
(4 files, last written Jul 4). **Refuse to save when the load came from the fallback, or
UPSERT per month instead of DELETE-all.**

### 10. Export ingest drops the expense side per item
`:6594-6601`: `verb=="sold"` only bumps scalar `spent`, never the item dict →
`bought_qty` always 0 on exports; comb/free-stock valuation at :4110 is dead on exports.
**setdefault the item and record bought_qty/net_coins.**

### 11. Honey payout: marker-after-loop double-pay
`:3694` unguarded `add_coins` in loop; marker advanced once at :3711 inside
`except: pass`. One failure → earlier harvesters re-paid on next upload. **Advance
`harvest_last_ts` per-row; guard each credit.**

### 12. Misc bot (MED)
- `_market_asset_value` (:9834): NO sell_qty NULL guard → 64× book value (web side
  guards this exact case at Restocker_web.py:551). Mirror the guard.
- `:3300` `p/(_qty or 1)`: blank/0 qty stores stack price as piece price. Store NULL.
- csn_profiles.json bypasses the allowlist (events.py:272 outside the .csv gate) and
  writes display_name verbatim into the GLOBAL alias store → sanitize + gate.
- Alias/lore injection: `@everyone`/markdown learned into aliases (verified);
  `_low_note` and market-id interpolations sent without `allowed_mentions` →
  use `_NO_MASS_MENTIONS` (exists), sanitize base names at :3365.
- CSV formula injection in generated stock_<market>_full.csv (:3737) — prefix `=+-@`.
- Dedup marker `csn_autoreport_seen` set BEFORE work succeeds (:3869) → failed run's
  re-drop discarded for 15 min. Set after success.
- CSN auto-registration (:4171): every new item enters catalog as stackable/64 (the
  exact 64× bug) and coin rounded to int (1.25 → 1). Pass _detect_stack_size + float.
- `_market_id_by_code` (:6420) is dead code — wire it into the :4072 fallback so a
  typo'd market_id with a valid code doesn't land in TEST.
- Stale stock rows never cleared on rescan (:3745 upsert-only) → remove rows absent
  from the current scan.
- market_stock_history inserts swallowed (`Restocker_db.py:2036 except: pass`).
- `_parse_period_transactions` silently drops rows with blank/short timestamps (:6537).
- Month attribution: mod files 35d-old sales into CURRENT month (:995 mod-side) while
  sale_day says the real month — the two views disagree. Stock `timestamp_iso` never
  read → history keyed on ingest time.
- `_load_csn_history` uses csn_history.yml for main; `_load_csn_for_market("main")`
  uses csn_history_main.yml — two different backups for the same market.
- `# PERIOD` is 2 fields, parser wants 3 → period_from/to always None (title loses its
  range). `_extract_market_info` splits without CSV quoting (comma in code truncates).
- `_parse_monthly_csv:6667` same-ts blocks REPLACE instead of merge.

---

## PHASE 3 — Mod bugs (Java)

### 13. Title-screen Save WIPES csn_config.json  ⚠ REGRESSION from today's ensureConfigDir
CsnSettingsScreen.java:50-97 + CsnExportClient.java:172-177,445-464. At title screen
loadConfig has never run (tick loader :203 needs player != null) → statics empty →
Save now SUCCEEDS and rewrites the file with blank webhook/id/code/owner + EMPTY
brew_aliases. Also: visiting settings at title screen sets configDir, which permanently
suppresses the config load for the session → F7 stock scan posts NOWHERE (F6 export is
safe — start() calls loadConfig).
**Fix: load config into statics in CsnSettingsScreen.init() if not loaded; guard the
tick loader with a configLoaded boolean, not configDir == null.**

### 14. Sales marked seen even when the CSV write FAILS → permanent loss
CsnExportClient.java:599-610: write failure only printStackTrace's, then
`seen.addAll(added); saveSeen(...)` runs anyway; on a completed run `csn clear` deletes
the server copy. Sales exist nowhere and can never be re-captured.
**Only add to .seen / advance flushedIdx after the append succeeds.**

### 15. THE EXPORT STALL — two concrete mechanisms (long-standing field bug)
- No retry: after sending `csn history N`, requestedPage == pendingPage forever
  (:283-287); a throttled/eaten command = guaranteed 45s stall-out.
  **Fix: if lastActivityAtMs > ~12s with a request outstanding, reset requestedPage=0
  to re-send (bounded retries).**
- `Page X / ?` deadlock: PAGE_RE accepts `?` but parseInt throws into silent catch →
  totalPages=0 → NEITHER advance branch fires (:510-533). Deterministic stall.
  **Fix: unknown total → still set pendingPage = currentPage+1; treat repeated/empty
  page as the end.**

### 16. loadSeen failure → empty set → duplicates + double hive payouts
:1421-1428: IOException reading .seen returns EMPTY set → everything re-appends and
re-posts as fresh, including hive wage lines. Plausible on Windows (file rewritten every
15s — ~P/5+1 full rewrites per run; two alts sharing .minecraft/sales race it).
**On read failure skip that flush; longer-term append-only .seen.**

### 17. Webhook 429: hive wage lines permanently lost
:689-696 fires csn-push + csn-hive concurrently (+ lands ~8s later) → invites the rate
limit. postHiveHarvest (:1392) returns mid-list, no retry, no spool; runFresh is
memory-only, .seen already marks exported, history cleared → harvesters silently unpaid.
**Serialize posts, honour Retry-After, spool unposted hive lines until 2xx.**

### 18. Misc mod (MED)
- Loose `PAGE_RE.find()` on every chat line (:507): any "Page 1 / 3" from another
  plugin can complete the run early → csn clear deletes UNREAD pages. Anchor to the CSN
  header decoration.
- Stock-scan lore pollution: SERVER_NOISE_RE is a 7-phrase blocklist; "Alex joined the
  game" becomes lore AND an "enchant" via translateEnchants → item renamed. Capture lore
  only between Item: and the price lines, or whitelist enchant-shaped lines.
- Export path doesn't split multi-line messages (stock path does, :493 vs :497) —
  latent 0-sales page.
- Last-page 1.5s flush window (:278) drops laggy tail entries, then clears.
- Corrupt csn_config.json: loadProfiles() sits INSIDE the try (:398) → also loses all
  profiles/aliases for the run. Move it after the catch.
- concludeVerify wipes .seen when verify sees nothing within 6s — but pages arrive at
  3s intervals on a just-spammed server → false "empty", .seen wiped, next run
  re-reads everything (pairs with #2's bot-side dedup gap).
- Silent catches that matter: resetSeen truncation (:972), stopStockScan claiming
  success on failed write (:726/884), writeDefaultConfig (:420), handleStockLine stock
  parse (:749), LandTracker.loadSeen (:394).
- `# MARKET` header only written when the file is NEW (:576, :1028) → mid-month config
  or code rotation breaks attribution/acceptance until file rollover. Re-emit every run.
- Same-hour repeat sale silently dropped by the hour-bucket hash (:1441) — see #2.
- Dead code: postToWebhook (:1257), enum AppendResult (:167). Stale text: "press K
  again" (:681), help screen documents the removed alias editor, warnWrongOwner javadoc
  says "refuse" but it now warns-and-continues.
- csvField doesn't quote on `\r` (:1476).

---

## Kept working / verified clean
- pyflakes: zero undefined names bot-wide (except known-dead plt/mticker chart code —
  `_MATPLOTLIB_OK` is permanently False; charts have never run).
- Mod tree compiles (build/classes newer than last source edit, Java 21, no stragglers
  from the removed alias editor; all @Override placements correct).
- Encoding round-trips: `§` codes, `greyhame's` U+2019, commas/quotes in names all
  survive csvField → python csv. ASCII-vs-typographic apostrophe fails CLOSED.
- Existing aliases are never overwritten by learning (verified).
- Filename contract complete: nothing the mod sends is ignored except stock
  `timestamp_iso`; no bot pattern is unproduced. stock_full/restock_needed CSVs the BOT
  generates do not re-ingest (no loop).
- Dividends/manager-override month-idempotency held under forged-file testing.

## Agent handles (continue with SendMessage if needed)
- bot-side: a50bfb1dcaa725a1a · mod-side: a73cfada8e8f9b525 · integration: af7085457020c0f74
