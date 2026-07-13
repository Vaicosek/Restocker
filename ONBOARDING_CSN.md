# CSN mod — re-setup (config cleared)

Copy/paste-friendly steps for anyone whose `csn_config.json` got wiped and whose
data stopped showing up. Do them in order — the usual reason "my CSN data isn't
showing" is either the webhook is blank or the market code is missing.

## 1. Grab your Discord webhook link
1. In Discord, open the server → **Server Settings → Integrations → Webhooks**.
2. Use the existing webhook for your CSN reports channel (click it → **Copy Webhook URL**),
   or **New Webhook**, point it at the reports channel, then **Copy Webhook URL**.
3. Keep that URL on your clipboard — it looks like
   `https://discord.com/api/webhooks/123.../abc...`.

## 2. Get your Market Code
In the Discord server (with the bot present) run:

```
/market_code market_id:<your market>
```

It replies with a short code. Copy it. (If you don't have a market yet, a manager
runs `/market add market_id:<name>` first.)

## 3. Put it back into the mod
1. In Minecraft: **Mods** (Mod Menu) → **CSN Export** → the **config** button
   (gear/arrow). This opens **CSN Export Settings**.
2. Fill in:
   - **Discord Webhook URL** → paste the URL from step 1.
   - **Market ID** → your market name (e.g. `main`). Must match what you used in `/market_code`.
   - **Market Code** → paste the code from step 2.
3. Click **Save**.

Config is written to `.minecraft/sales/csn_config.json`, so it survives future restarts.

## 4. Bind the export key (required — the mod does nothing until you do)
**Options → Controls → Key Binds**, find the **CSN Export** category and bind
**"Export CSN History"** to a key. There's a second bind, **"Scan Shop Stock
(toggle)"**, for scanning barrel stock — optional. The mod won't export anything
until "Export CSN History" has a key assigned.

## 5. Run it
Join the server, press your **CSN Export** key. It scrapes your full `/csn history`,
saves CSVs to `.minecraft/sales/`, and (because the webhook is set) auto-posts the
monthly report to Discord. You'll see `[CSN] Export started…` then `✅ Report posted
to Discord!` in chat.

## Still not showing? Quick checks
- **Chat says "auto-post enabled"?** When you join, the mod prints how many aliases/
  profiles loaded and whether auto-post is on. If it doesn't say auto-post enabled,
  the webhook field is still blank — redo step 3.
- **Report got rejected for a bad/missing code?** The bot replies with
  "⛔ CSN report … rejected: missing/invalid market code." Fix the **Market Code**
  (step 2), or have a manager bind your channel once with
  `/market set_channel market_id:<yours>` — after that no code is needed.
- **Data landed in the TEST market?** Unattributed uploads (no code, no channel
  binding) now go to the **TEST** market instead of the main one, so they won't
  show under your market. Set the code/binding and re-export.
- **Only see one report even though you exported twice?** That's intended now —
  identical reports posted within 15 min are de-duped.
