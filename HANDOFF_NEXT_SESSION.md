# V TECH / RESTOCKER — SESSION HANDOFF (2026-07-19)

Repo: `C:\Users\Vaicos\Desktop\AI\RestockerLocal` (bot+web) · Mod: `C:\Users\Vaicos\Desktop\AI\Sales\csn-mod-src-1.21.11`
Deploy: run deploy.bat (git push) → Wispbyte panel `wispbyte.com/client/servers/81401447/console` → Restart (AUTO_UPDATE pulls on boot; watch for "Global slash commands synced"). ALL work below is COMMITTED to the repo but NOT YET DEPLOYED.

## HOUSE RULES (owner: Vaicos)
- main (GEX) cap pinned to 100M by asset book value ALONE; treasury/inventory/sellables are BACKING, never additive to price. Stocks move on earnings only; admin actions = full_move re-anchors.
- All inputs must be HARD DATA (coins, chest scans, tp fees ÷100 = visits, CSN months). Never trust human-typed numbers when the mod can measure.
- Coins are CONSERVED (no minting). Payouts idempotent per month. Server is scam-heavy; trust is the product.
- Backing target = 50% of cap (15 cash + 25 assets + 10 fund). Grade GATED by collateral: A=50% backed, AA=60%, AAA=80%, BBB=30%, BB=15%. Vault arrears cap grade at BBB.
- V Tech Vault: 10% of every listed company's positive monthly net accrues as mandatory deposit (vault_due, auto). Item pledges count at 70% (liquidation haircut). /vault deposit|pledge|status.
- Bonds: company-level (rollup parent), ≥80% ITEM collateral, coupons from treasury.

## BUILT THIS SESSION (all committed)
Features: quality engine (traffic/orders/backing/history → rating, ±20% P/E, index weight); defensive ABX fund (BBB+ only, hard-collateral weights, monthly rebalance); /bond issue|buy|info|list|my + coverage watchdog + web bond board; /vote (weight=shares+GEX.PR); /suggest; /escrow; /vault; DRIP (/stock drip); /stock buyback; monthly investor report; ratings-change announcements; dashboard: rating badges, visitors stat, bulk item remove; /land feed_channel.
Security (23-cog agent audit, 6 criticals + ~30 highs fixed, patches 1–6): lands/hive/CSN feeds authenticated (webhook-only, TOFU lock `csn_allowed_posters`); override marker monotonic; IPO price ≤2× fundamental; loyalty caps; permanent dividend ledger (safe re-imports); bond payouts ledger-guarded per holder; funds-loop snapshot fix; admin repair double-pays fixed; /market_code seizure fixed; rollup needs parent consent; /config pinned to guild 954487497411403806; wipe/delete hygiene; hive claim-first settle + content-dedup; barrel = 54×stack_size; IGN anti-squat (money-bearing IGNs need manager); delist fund-crash + race fixed; overrides paid FROM MARKET TREASURY (owner's call); project-pay ping-pong dead (circular pairs = 0 pts, 500 pts/day cap); /shop_rename_item DELETED.

## POST-DEPLOY CHECKLIST
1. Watch console: "Global slash commands synced", no cog errors. New groups: /bond /vote /suggest /escrow /vault, /stock drip, /stock buyback, /land feed_channel.
2. `/land feed_channel` on the mod webhook channel. First CSN post auto-locks poster.
3. `/vault deposit market_id:main amount:6500000` (10% of ~65M lifetime net; MardURAK treasury ≈6.85M covers it).
4. Expect ratings post: GEX downgrade to ~BB/BBB under new 50% target (23% backed) — announce as new-rule recalibration. Path up: more sellable/cash.
5. Smoke test: small /stock buy, one order approval (override now deducts market treasury).

## AMAZONIA TEST (Jacob, owns 100%)
CSN earnings for amazonia ALREADY IN DB (mod-recorded — do NOT import Jacob's sheet; use it only as lie detector vs CSN months: he claims May ~807k, June ~1.04M). Inventory ALREADY scanned/live on dashboard. Steps: (1) /market go_public market_id:amazonia (prices off CSN history; IPO bound enforces model) (2) /stock set_params amazonia assets:<hive book value> sellable:<50% liquid hive figure — his hives sell at 60-70% median, owner set 50%> (3) add his land to sales/lands_config.json + rebuild mod + /land bind → tp traffic. Jacob's weekly data: ~430 visits/wk, bank 2.7M + land 838k ≈ 3.5M liquid, profits volatile (3 neg weeks/10).

## NEXT BUILDS (priority)
1. **/valuate <market_id>** — auto-gather earnings/inventory/hives/traffic/vault, run full model, bot AI writes analyst report w/ anomaly flags. Owner wants AI valuations, zero human input. Then run on amazonia.
2. **Money core pass** (deduct_coins dual-store SQLite+JSON consistency — root of "credited without debiting"; needs focused session). Includes land-bound treasury delta-tracking (LANDS sync currently OVERWRITES treasury, wiping bot-side deductions — payouts from main's treasury must be mirrored by in-game withdrawal until fixed).
3. Ops roadmap top picks: /deploy one-command pipeline; mod auto-posts CSN exports; owner morning digest; worker reliability score; auto-escalating bounties; stock velocity engine; auto-restock pilot; repricing suggestions; proof-of-backing weekly post; /invest panel; competitor price radar; web worker board (big bet).

## PARKED / KNOWN
- Bind V Tech market lands (amazonia, viridian, greyhames…) in lands_config.json + /land bind — traffic pillar reads 0 until then. Open each land inbox occasionally before clearing papers (fee math self-heals retroactively).
- Rebuild+install mod jar on Vaicos main instance (only MAIN/Boris has LandTracker). Build: Sales\build_and_install.bat.
- Delete empty "📈 Investor" role; delete Vaicos' two junk msgs in #vtech.
- Investor infra live: #dividend-reports 1500543246718206002 (read-only), #investor-chat 1500543251202052218, canonical @Investor + @Shareholder applied, INVESTORS category private.
- ETF_MIN_GRADE=BBB; INDEX rebases on composition change (index ≠ price, by design). QUALITY_* / STOCK_BACK_* / STOCK_RETAINED_EARNINGS_PCT / VAULT_PLEDGE_HAIRCUT all env-tunable.
- amazonia_earnings_import.csv exists (Jacob rollup) — cross-check only, do not import.
