# SW Trade Network ↔ V Helper — Integration Options

## The core constraint (why plain mirroring breaks)

Discord **interactive components (buttons/selects) route to the bot that posted the message.**
When a worker clicks a button, Discord sends the interaction to *that* application. So:

- If SW Trade Network (SWTN) copies my order text into 55 servers, my bot isn't in those
  servers → my buttons can't fire there, and I never learn who clicked.
- You **cannot** re-send another bot's working buttons. Only the bot that posts a message can
  attach components that route back to itself.

There is exactly **one exception**: a **link button** (style = Link, just a URL). It has no
bot behind it — it only opens a URL — so it works in *any* server with no bot present.

That gives two viable designs.

---

## Path A — Link-out (works TODAY, zero dependency on SWTN's API)

My bot posts the order to the network as a normal embed **plus a Link button** →
`🔗 Claim / View order`. The URL deep-links back to **my** server or dashboard, where my bot
*does* live and the real interactive UI (claim, mats/recipe tickets, trust) works normally and
natively captures the worker's Discord ID.

```
[Order embed broadcast to all 55 servers by SWTN]
  Diamond Armor Set ×30 — pay 40k — Amazonia
  🔗 Claim this order   ← Link button → https://discord.com/channels/<myGuild>/<chan>/<msg>
                                       (or https://dashboard.vaicosmarket.com/claim/<id>)
```

- **Pros:** works immediately, no cooperation needed, keeps all my claim/ticket/trust logic
  exactly as-is, and I always get the real Discord ID because they end up in my server.
- **Cons:** one extra hop — the worker leaves the network post to come to me. (This is the
  "easier but not that funny" option from the chat. It's the right first step.)

---

## Path B — Two-bot API + webhook (seamless, needs Jima to build the API)

SWTN renders **its own** claim button (SWTN-owned, so it works in all 55 servers). On click,
SWTN captures the clicker and **calls my bot back** with their identity. The two UIs act as one
purely through data transfer — exactly the model described in the chat.

```
my bot ──POST order──▶ SWTN API ──broadcasts w/ SWTN's own Claim button──▶ 55 servers
worker clicks Claim (any server) ──▶ SWTN grabs their Discord ID
SWTN ──POST callback──▶ my bot's webhook  { order, worker_id, action }
my bot opens ticket / marks claimed / trust check, replies with a link to show the worker
```

### What I need the SWTN API to let me do (this is the "spec" Jima asked for)

**1. Post / broadcast an order** — `POST https://<swtn>/api/orders`
```jsonc
// headers: Authorization: Bearer <my api key>
{
  "external_id": "vhelper-8123",         // my order id, echoed back on every callback
  "title": "Diamond Armor Set ×30",
  "body": "Clean, Unbreaking III. Pay 40k. Deliver to Amazonia spawn.",
  "category": "Job Listing",             // maps to a network forum tag
  "reward": "40000 coins",
  "callback_url": "https://<me>/api/swtn/callback",
  "actions": ["claim"]                   // buttons SWTN should render (owned by SWTN)
}
// returns: { "network_post_id": "…", "ok": true }
```

**2. Claim callback (the important one)** — SWTN → `POST https://<me>/api/swtn/callback`
```jsonc
// headers: X-SWTN-Signature: <hmac so I can verify it's really SWTN>
{
  "external_id": "vhelper-8123",
  "network_post_id": "…",
  "action": "claim",
  "worker_discord_id": "1203738126850461738",   // ← the whole point
  "worker_username": "SomeWorker",
  "source_server_id": "910575314357338202"       // which network server they clicked in
}
// my bot replies 200 with what to show the worker:
// { "ok": true, "message": "Claimed! Open your ticket:", "url": "https://discord.gg/…/<ticket>" }
```
This one callback alone solves the hard part (knowing *who* is working on it). Everything else
is optional.

**3. Update / close an order** — `POST https://<swtn>/api/orders/{network_post_id}`
`{ "status": "claimed" | "fulfilled" | "cancelled", "note": "…" }`
so SWTN edits the mirrored posts across all servers (mark claimed, filled, remove).

**4. Auth (both directions)**
- Me → SWTN: an API key/bearer token.
- SWTN → me: an HMAC signature (shared secret) on the callback so I can verify it's genuine
  and reject spoofed claims.

### Minimum viable (if Jima wants the smallest possible build)
Just **#1 (post with a claim button)** + **#2 (claim callback with the worker's Discord ID)**.
With those two I can do all my ticketing/trust/claiming on my side. #3 and #4 are polish.

---

## Recommendation

1. **Now:** ship Path A (link-out). It's a Link button + a deep URL — no dependency, works in
   all 55 servers today, and reuses my existing claim UI. Not blocked on anyone.
2. **Later:** when Jima builds the API, add Path B for the seamless in-place claim. Same order
   data, just a nicer front door.

Both can coexist: post the embed with SWTN's native Claim button *and* a link-out fallback.
