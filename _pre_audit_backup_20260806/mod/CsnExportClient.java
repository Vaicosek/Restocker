package com.vaicos.csnexport;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.api.EnvType;
import net.fabricmc.api.Environment;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.keybinding.v1.KeyBindingHelper;
import net.fabricmc.fabric.api.client.message.v1.ClientReceiveMessageEvents;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.option.KeyBinding;
import net.minecraft.client.util.InputUtil;
import org.lwjgl.glfw.GLFW;
import net.minecraft.text.Text;
import net.minecraft.util.Identifier;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.HitResult;

import java.io.IOException;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

@Environment(EnvType.CLIENT)
public class CsnExportClient implements ClientModInitializer {

    private static final int     PERIOD_START_DAY     = 1;
    private static final long    PAGE_REQUEST_DELAY_MS = 3000;  // ~0.33 commands/sec — slowed down after spam-kick on a public server
    // Was 1.5s: on a laggy server the LAST page's entries can trail its header by more
    // than that, so the tail entries were dropped AND the history then cleared. 5s costs
    // nothing on a healthy server (the run just ends 3.5s later) and keeps the tail.
    private static final long    LAST_PAGE_FLUSH_MS    = 5_000;
    // No fixed page/time cap. The export runs until the LAST page arrives, however
    // many there are (a 230-page history at 3s/page is ~12 min — the old 10-min cap
    // chopped it off AND still cleared the un-fetched pages, losing sales). Instead
    // we only bail if the server goes silent (a genuine hang), never on a healthy but
    // long export. The absolute backstop is huge and should never trigger.
    private static final long    STALL_TIMEOUT_MS      = 45_000;            // no new page for 45s = server not responding
    private static final long    HARD_TIMEOUT_MS       = 60 * 60 * 1_000;   // 60-min absolute backstop (effectively "no timeout")
    private static final long    FLUSH_INTERVAL_MS     = 15_000;            // persist collected entries to disk every 15s (crash-safe)
    private static final int     MAX_CLEAR_ATTEMPTS    = 3;      // retries if /csn clear doesn't empty history
    private static final long    VERIFY_DELAY_MS       = 2_000;  // wait after sending clear before re-checking
    // Was 6s: on a just-spammed server pages arrive at ~3s intervals, so a slow first page
    // read as "history empty" and WIPED .seen — the next run then re-read everything.
    // 10s base + the deadline is EXTENDED whenever a page header arrives during verify.
    private static final long    VERIFY_RESPONSE_MS    = 10_000; // how long to wait for the verify read's reply
    /** client.player blinks to null on dimension changes, respawns and loading screens.
     *  Only a gap longer than this counts as a genuine logout worth re-arming for. */
    private static final long    REJOIN_GRACE_MS       = 60_000;
    /** Hard floor between two AUTO exports, whatever the client does. Belt-and-braces
     *  behind REJOIN_GRACE_MS: an AFK alt that reconnects on a loop must never be able
     *  to spam the webhook. Manual F6 is never rate-limited. */
    private static final long    AUTO_EXPORT_MIN_GAP_MS = 30 * 60_000L;
    private static final long    PAGE_RETRY_MS         = 12_000; // re-send a page request if no page arrived for this long
    private static final int     MAX_PAGE_RETRIES      = 3;      // bounded re-sends per page (throttled/eaten command)
    private static final long    STOCK_IDLE_SAVE_MS    = 30_000; // stock scan auto-saves this long after the last captured shop

    private static final Pattern PAGE_RE = Pattern.compile(
            "Page\\s+(\\d+)\\s*/\\s*(\\d+|\\?)");

    private static final Pattern ENTRY_RE = Pattern.compile(
            "^\\s*[+\\-]?\\s*(.+?)\\s+(bought|sold)(?:\\s+you)?\\s+(\\d+)x\\s*(.+?)\\s+" +
            "(?:(\\d+)d\\s*)?(?:(\\d+)h\\s*)?(?:(\\d+)m\\s*)?(?:(\\d+)s\\s*)?ago\\s*" +
            "\\(([+\\-])([\\d,]+(?:\\.\\d+)?)\\s+Coins\\)\\s*$",
            Pattern.CASE_INSENSITIVE);

    private static final Pattern CSN_FOOTER_RE = Pattern.compile(
            ".*(To remove all entries|/csn clear|csn history).*",
            Pattern.CASE_INSENSITIVE);

    // ── live shop-stock capture ("Shop Information" block from clicking a barrel) ──
    private static final Pattern SHOP_HEADER_RE = Pattern.compile("^\\s*Shop Information:?\\s*$", Pattern.CASE_INSENSITIVE);
    private static final Pattern SHOP_OWNER_RE  = Pattern.compile("^\\s*Owner:\\s*(.+?)\\s*$", Pattern.CASE_INSENSITIVE);
    private static final Pattern SHOP_STOCK_RE  = Pattern.compile("^\\s*Stock:\\s*([\\d,]+)\\s*$", Pattern.CASE_INSENSITIVE);
    private static final Pattern SHOP_ITEM_RE   = Pattern.compile("^\\s*Item:\\s*(.+?)\\s*$", Pattern.CASE_INSENSITIVE);
    // server chat that must never be captured as shop lore
    private static final Pattern SERVER_NOISE_RE = Pattern.compile(
            "(?i)(you can'?t (sell|buy) here|got stabbed|has been killed|was given to|sacrificed to|is now AFK|no longer AFK)");
    private static final Pattern SHOP_PRICE_RE  = Pattern.compile("^\\s*(?:(Buy|Sell)\\s+)?(\\d+)\\s+for\\s+([\\d,]+(?:\\.\\d+)?)\\s+Coins\\s*$", Pattern.CASE_INSENSITIVE);

    // ONE code-strip rule for BOTH the sales path and the stock path. The sales path used
    // to strip hex-only codes (#aFe) while stock stripped any alnum code (#akQ), so the
    // same item exported as "Potion#akQ" in sales but "Potion" in stock and never matched.
    private static final Pattern ITEM_CODE_RE = Pattern.compile("#[a-zA-Z0-9]{1,6}$");

    static Path configDir;
    /** True once loadConfig() has actually READ (or created) csn_config.json into the
     *  statics. This — not configDir — must gate every save and the tick loader: at the
     *  title screen configDir can be set (settings screen) while the statics are still
     *  empty, and saving then would wipe the real file (the "title-screen Save wipes
     *  csn_config.json" regression). */
    static boolean configLoaded = false;
    static String discordWebhook = "";
    static String marketId   = "";
    static String marketCode = "";
    /** IGN this config was set up for. csn_config.json lives in .minecraft, so it survives
     *  every mod reinstall and travels whenever a setup is copied or handed to someone
     *  else — which silently files THEIR sales under the original owner's market. Binding
     *  the config to an account makes that impossible instead of merely unlikely. Empty
     *  means "not yet claimed"; it is adopted automatically on first use. */
    static String ownerIgn = "";
    /** Minutes after login before the export auto-starts. 0 disables. Configurable via
     *  "auto_export_minutes" in csn_config.json (absent → default 5). */
    static int autoExportMinutes = 5;
    static Map<String, String> brewAliases = new HashMap<>();

    // ── item profile database (csn_profiles.json) ────────────────────────
    // key = baseItemName@sellPrice  (e.g. "Potion@275", "Diamond Leggings@1000")
    // value = JsonObject { display_name, effects[], lore[], buy_price, sell_price, known_hashes[] }
    static Map<String, JsonObject> itemProfiles    = new LinkedHashMap<>();
    static Map<String, String>     hashToProfileKey = new HashMap<>();   // raw hash → profileKey

    private KeyBinding exportKey;
    private boolean running = false;

    private long startedAtMs;
    private long nextActionAtMs;
    private long lastPageReceivedAtMs;
    private long lastActivityAtMs;   // last time ANY page header arrived — drives the stall detector
    private boolean announcedTotal;  // so we announce the detected page count exactly once per run
    private int  currentPage;

    // ── crash-safe incremental persistence ───────────────────────────────────
    // Entries used to be written only at finish(), so a long export (230 pages ≈
    // 12 min) that got interrupted before finishing left ZERO files. We now flush
    // collected entries to the CSV every FLUSH_INTERVAL_MS, deduped via .seen, so an
    // interrupted run keeps what it fetched and a re-run resumes without double-count.
    private ExportTargets runTargets;                       // resolved once at start
    private final java.util.List<Entry> runFresh = new ArrayList<>();  // fresh entries persisted this run (for monthly + summary)
    private int  flushedIdx;                                // how many of `entries` are already on disk
    private double runProfit, runLoss;                      // accumulated for the run-summary footer
    private long lastFlushAtMs;
    private int  totalPages;
    private int  requestedPage;
    private int  pendingPage;
    private long worldJoinAtMs;                // when the player appeared in-world (0 = not in world)
    private boolean autoExportFired;           // one auto-export per login, not per tick
    private long playerGoneSinceMs;            // when client.player went null (0 = present)
    private long lastAutoExportAtMs;           // hard rate-limit backstop for auto-export
    private long lastRequestAtMs;              // when the outstanding "csn history N" was sent
    private int  pageRetries;                  // bounded re-sends of the outstanding page request
    private int  entriesAtLastHeader;          // entries.size() when the previous page header arrived
    private boolean marketHeaderWrittenThisRun; // "# MARKET" re-emitted once per run on existing files
    private boolean flushErrorNotified;        // rate-limit flush failure chat spam to once per run

    // ── post-clear verification state ────────────────────────────────────────
    private boolean verifyMode = false;
    private boolean verifyRequested = false;
    private boolean verifyGotData = false;
    private boolean verifyGotUnseen = false;      // verify saw a sale we never exported
    private Set<String> verifySeen = null;        // .seen snapshot for the verify read
    private long    verifyDeadlineMs;
    private int     clearAttempts = 0;
    private ExportTargets lastTargets;

    // ── live shop-stock scan state ───────────────────────────────────────────
    private KeyBinding stockKey;
    private boolean stockScanning = false;
    private final Map<String, StockShop> stockShops = new LinkedHashMap<>();
    private long stockLastCaptureMs = 0;   // 0 = nothing captured yet, no auto-save timer running
    private boolean      inShopBlock    = false;
    private String       pOwner, pItem, pItemRaw;
    private long         pStock         = -1;
    private int          pBuyQty = 0, pSellQty = 0;
    private double       pBuyPrice = -1, pSellPrice = -1;
    private int          pPriceLineCount = 0;
    private String       pPos           = null;   // clicked barrel's block position (multi-barrel key)
    private List<String> pLore          = new ArrayList<>();

    private final List<Entry> entries = new ArrayList<>();
    private final Set<String> seenChatLinesThisRun = new HashSet<>();
    private String sellerName = "UNKNOWN";
    private String runTimestampIso;

    record Entry(
            String actor,
            String seller,
            String verb,
            int    quantity,
            String item,      // display base: raw name with the #code stripped
            String itemRaw,   // EXACTLY as CSN printed it (keeps the #code) — this is what
                              // profiles/aliases are keyed by, and what the sale uid hashes
            double amountCoins,
            String timestampIso
    ) {}

    record StockShop(String owner, String item, String itemRaw, long stock,
                     int buyQty, double buyPrice, int sellQty, double sellPrice,
                     List<String> lore, String tsIso) {}

    record ExportTargets(Path dataFile, Path seenFile, Path monthlyFile) {}

    /** configDir is only assigned on world join, but the settings screen can be opened
     *  from the TITLE screen via Mod Menu. Every save path must call this first or the
     *  write is silently discarded and the player's edit vanishes. */
    static void ensureConfigDir() { ensureConfigDir(MinecraftClient.getInstance()); }

    static void ensureConfigDir(MinecraftClient client) {
        if (configDir == null && client != null && client.runDirectory != null)
            configDir = client.runDirectory.toPath().resolve("sales");
    }

    @Override
    public void onInitializeClient() {
        KeyBinding.Category csnCategory = KeyBinding.Category.create(Identifier.of("csnexport", "category"));
        exportKey = KeyBindingHelper.registerKeyBinding(new KeyBinding(
                "key.csnexport.export",
                InputUtil.Type.KEYSYM,
                // Was -1 (UNBOUND). Every new install therefore did nothing at all when
                // the player "pressed the key" — there wasn't one — with no error shown.
                // F6/F7/F8 are free in vanilla, so these steal nothing.
                GLFW.GLFW_KEY_F6,
                csnCategory
        ));

        stockKey = KeyBindingHelper.registerKeyBinding(new KeyBinding(
                "key.csnexport.stock",
                InputUtil.Type.KEYSYM,
                GLFW.GLFW_KEY_F7,
                csnCategory
        ));

        LandTracker.init(csnCategory);   // lands (claims) treasury + inbox tracking

        ClientTickEvents.END_CLIENT_TICK.register(this::onEndTick);
        ClientReceiveMessageEvents.GAME.register(this::onReceiveGameMessage);
        ClientTickEvents.START_CLIENT_TICK.register(client -> {
            // Gate on configLoaded, NOT configDir: opening the settings screen at the title
            // screen sets configDir, which used to permanently suppress this load for the
            // whole session (F7 stock scans then posted nowhere).
            if (!configLoaded && client.player != null)
                loadConfig(client);
        });
    }

    private void onEndTick(MinecraftClient client) {
        if (client.player == null) {
            // Note WHEN the player vanished, but don't re-arm yet. client.player goes
            // null constantly in normal play — dimension changes, respawns, brief lag,
            // the loading screen. Re-arming on every blink is what made an AFK alt
            // auto-export every few minutes forever (observed: runs 3m34s apart).
            if (playerGoneSinceMs == 0) playerGoneSinceMs = System.currentTimeMillis();
            return;
        }

        long now = System.currentTimeMillis();

        // A REAL logout = gone for more than a moment. Only that re-arms the auto-export.
        if (playerGoneSinceMs > 0) {
            if ((now - playerGoneSinceMs) >= REJOIN_GRACE_MS) worldJoinAtMs = 0;
            playerGoneSinceMs = 0;
        }

        if (worldJoinAtMs == 0) {
            worldJoinAtMs   = now;
            autoExportFired = false;
        }

        // ── Auto-export: start the history read a few minutes after login ────────
        // ONLY when this install is actually CONFIGURED (market_id AND market_code AND
        // webhook all set). A fresh/empty install must never auto-run — otherwise a new
        // player's sales would silently flow to whatever default someone shipped. They
        // set up CSN settings first; until then the mod does nothing on its own.
        if (!autoExportFired && autoExportMinutes > 0 && configLoaded
                && (now - worldJoinAtMs) >= autoExportMinutes * 60_000L
                && (lastAutoExportAtMs == 0 || (now - lastAutoExportAtMs) >= AUTO_EXPORT_MIN_GAP_MS)
                && !running && !verifyMode && !stockScanning
                && !marketId.isEmpty() && !marketCode.isEmpty() && !discordWebhook.isEmpty()) {
            autoExportFired    = true;
            lastAutoExportAtMs = now;
            client.player.sendMessage(Text.literal(String.format(
                "[CSN] Auto-export starting (%d min after login) — press F6 anytime to run it manually; "
                + "set \"auto_export_minutes\": 0 in csn_config.json to turn this off.",
                autoExportMinutes)), false);
            if (!ownerMatches(client)) warnWrongOwner(client);
            start(client);
            return;
        }

        if (exportKey.wasPressed()) {
            if (running) {
                client.player.sendMessage(
                    Text.literal(String.format("[CSN] Already running… (page %d / %d)", currentPage, totalPages)), false);
            } else if (verifyMode) {
                client.player.sendMessage(
                    Text.literal("[CSN] Verifying /csn clear… one moment."), false);
            } else {
                // Alt accounts are normal — one person may run several. A mismatch is
                // worth SAYING, but blocking the export just stops real sales being
                // reported. The market code / channel binding is the actual credential.
                if (!ownerMatches(client)) warnWrongOwner(client);
                start(client);
            }
            return;
        }

        if (stockKey.wasPressed()) {
            if (!ownerMatches(client)) warnWrongOwner(client);   // warn only, never block
            if (running || verifyMode) {
                client.player.sendMessage(Text.literal("[CSN] Busy exporting - try the stock scan after it finishes."), false);
            } else if (!stockScanning) {
                startStockScan(client);
            } else {
                stopStockScan(client);
            }
            return;
        }

        // Auto-save the stock scan once the player has stopped clicking shops.
        // Timer only runs after at least one shop was captured, so an idle
        // scan with nothing in it never posts an empty report.
        if (stockScanning && stockLastCaptureMs > 0
                && (now - stockLastCaptureMs) >= STOCK_IDLE_SAVE_MS) {
            stopStockScan(client);
            return;
        }

        if (verifyMode) {
            tickVerify(client, now);
            return;
        }

        if (!running) return;

        // Stall guard: only give up if the server stopped responding (no new page for
        // STALL_TIMEOUT_MS), never because the history is simply long. Ends as "stalled"
        // so finish() KEEPS the history (no /csn clear) and no sales are lost.
        if (lastActivityAtMs > 0 && (now - lastActivityAtMs) > STALL_TIMEOUT_MS) {
            finish(client, "stalled");
            return;
        }
        if ((now - startedAtMs) > HARD_TIMEOUT_MS) {   // absolute backstop, should never trigger
            finish(client, "timeout");
            return;
        }

        // Crash-safe: persist collected entries every FLUSH_INTERVAL_MS so an
        // interrupted export keeps what it fetched instead of losing everything.
        if ((now - lastFlushAtMs) >= FLUSH_INTERVAL_MS && flushedIdx < entries.size()) {
            lastFlushAtMs = now;
            flushCollected();
        }

        if (lastPageReceivedAtMs > 0 && (now - lastPageReceivedAtMs) >= LAST_PAGE_FLUSH_MS) {
            finish(client, "complete");
            return;
        }

        // Retry an eaten/throttled page request: with a request outstanding
        // (requestedPage == pendingPage) and no page for PAGE_RETRY_MS, re-arm the send
        // branch below (bounded). Without this, one lost command meant requestedPage ==
        // pendingPage forever and a guaranteed 45s stall-out.
        if (lastPageReceivedAtMs == 0 && requestedPage > 0 && requestedPage == pendingPage
                && (now - lastRequestAtMs) > PAGE_RETRY_MS && pageRetries < MAX_PAGE_RETRIES) {
            pageRetries++;
            requestedPage = 0;   // re-arms pendingPage != requestedPage below
            if (client.player != null) {
                client.player.sendMessage(Text.literal(String.format(
                    "[CSN] No reply for page %d — re-sending (retry %d/%d)…",
                    pendingPage, pageRetries, MAX_PAGE_RETRIES)), true);
            }
        }

        if (lastPageReceivedAtMs == 0 && now >= nextActionAtMs && pendingPage != requestedPage) {
            sendCommand(client, "csn history " + pendingPage);
            requestedPage   = pendingPage;
            lastRequestAtMs = now;
            nextActionAtMs  = now + PAGE_REQUEST_DELAY_MS;
        }
    }

    private static void warnMarketConfig(MinecraftClient client) {
        if (client.player == null) return;
        boolean hasId   = !marketId.isEmpty();
        boolean hasCode = !marketCode.isEmpty();
        if (hasId && !hasCode) {
            client.player.sendMessage(Text.literal(
                "[CSN] Market ID set but no Market Code. On a channel not bound to this market the bot "
                + "will reject the report - get your code from /market settings in Discord, or post to a "
                + "channel bound there."), false);
        } else if (!hasId && hasCode) {
            client.player.sendMessage(Text.literal(
                "[CSN] Market Code set but no Market ID. Set the Market ID in CSN settings so the bot "
                + "knows which market to attribute."), false);
        }
    }

    private void start(MinecraftClient client) {
        if (running) {
            client.player.sendMessage(Text.literal("CSN export already running"), false);
            return;
        }

        loadConfig(client);
        warnMarketConfig(client);

        running               = true;
        startedAtMs           = System.currentTimeMillis();
        nextActionAtMs        = startedAtMs + 500;
        lastPageReceivedAtMs  = 0;
        lastActivityAtMs      = startedAtMs;   // arm the stall detector; reset on each page below
        announcedTotal        = false;
        currentPage           = 0;
        totalPages            = 0;
        requestedPage         = 0;
        pendingPage           = 1;
        entries.clear();
        seenChatLinesThisRun.clear();
        verifyMode           = false;
        verifyRequested      = false;
        sellerName           = client.player.getGameProfile().name();
        runTimestampIso      = Instant.now().toString();

        runTargets           = resolveTargets(client);
        runFresh.clear();
        flushedIdx           = 0;
        runProfit = runLoss  = 0;
        lastFlushAtMs        = startedAtMs;
        pageRetries          = 0;
        entriesAtLastHeader  = 0;
        marketHeaderWrittenThisRun = false;
        flushErrorNotified   = false;

        sendCommand(client, "csn history 1");
        requestedPage   = 1;
        lastRequestAtMs = startedAtMs;
        client.player.sendMessage(Text.literal("[CSN] Export started…"), false);
    }

    static void loadConfig(MinecraftClient client) {
        configDir = client.runDirectory.toPath().resolve("sales");
        Path configFile = configDir.resolve("csn_config.json");

        brewAliases    = new HashMap<>();
        // Reset ALL identity fields, not just the webhook: these are static, so a value
        // deleted from the file used to linger in memory from the previous load and keep
        // filing sales under a market the config no longer names.
        discordWebhook = "";
        marketId       = "";
        marketCode     = "";
        ownerIgn       = "";

        if (!Files.exists(configFile, LinkOption.NOFOLLOW_LINKS)) {
            writeDefaultConfig(configFile);
            configLoaded = true;   // empty statics ARE the file's (default) content
            loadProfiles();        // profiles may exist even when the config is fresh
            return;
        }

        try {
            String json = Files.readString(configFile, StandardCharsets.UTF_8);
            JsonObject root = JsonParser.parseString(json).getAsJsonObject();

            if (root.has("discord_webhook") && !root.get("discord_webhook").isJsonNull())
                discordWebhook = root.get("discord_webhook").getAsString().trim();

            if (root.has("market_id") && !root.get("market_id").isJsonNull())
                marketId = root.get("market_id").getAsString().trim();

            if (root.has("market_code") && !root.get("market_code").isJsonNull())
                marketCode = root.get("market_code").getAsString().trim();

            if (root.has("owner_ign") && !root.get("owner_ign").isJsonNull())
                ownerIgn = root.get("owner_ign").getAsString().trim();

            autoExportMinutes = 5;   // default: auto-run 5 min after login (when configured)
            if (root.has("auto_export_minutes") && !root.get("auto_export_minutes").isJsonNull()) {
                try { autoExportMinutes = Math.max(0, root.get("auto_export_minutes").getAsInt()); }
                catch (Exception ignored) {}
            }

            if (root.has("brew_aliases") && root.get("brew_aliases").isJsonObject()) {
                JsonObject aliases = root.getAsJsonObject("brew_aliases");
                for (Map.Entry<String, com.google.gson.JsonElement> e : aliases.entrySet()) {
                    String code = e.getKey().strip();
                    String name = e.getValue().getAsString().strip();
                    if (!code.isEmpty() && !name.isEmpty())
                        brewAliases.put(code, name);
                }
            }

            // The config parsed cleanly — only NOW is it safe for saveConfig() to write.
            // On a parse failure configLoaded stays false, so a Save can't flush the
            // just-reset empty statics over a merely-corrupt (recoverable) file.
            configLoaded = true;

            // Claim an unclaimed config for whoever is logged in now. Existing setups have
            // no owner_ign, so they adopt the current player on first load and keep working
            // — only a DIFFERENT account later gets stopped. Runs AFTER the aliases are
            // parsed and configLoaded is set: the adoption save used to fire with
            // brewAliases still empty, silently wiping every alias from the file.
            if (ownerIgn.isEmpty() && !marketId.isEmpty() && client.player != null) {
                ownerIgn = client.player.getGameProfile().name();
                saveConfig();
                client.player.sendMessage(Text.literal(
                    "[CSN] This config is now bound to " + ownerIgn + " for market '" + marketId
                    + "'. Another account using this folder will be asked to update it."), false);
            }

            if (client.player != null) {
                client.player.sendMessage(
                    Text.literal(String.format("[CSN] Config — %d alias(es), %d profile(s)%s",
                        brewAliases.size(), itemProfiles.size(),
                        discordWebhook.isEmpty() ? "" : ", auto-post enabled")), false);
            }

        } catch (Exception e) {
            System.err.println("[CSN] Failed to load csn_config.json: " + e.getMessage());
            if (client.player != null)
                client.player.sendMessage(
                    Text.literal("[CSN] Could not read csn_config.json: " + e.getMessage()), false);
        }
        // OUTSIDE the try: a corrupt csn_config.json must not also cost this run every
        // profile/alias in the separate csn_profiles.json (it used to sit inside the try,
        // so the catch above skipped it).
        loadProfiles();
    }

    private static void writeDefaultConfig(Path configFile) {
        // Every identity field ships EMPTY. A fresh install must not export anywhere
        // until the player fills these in (CSN settings / this file) — auto-export
        // stays inert while any of webhook/market_id/market_code is blank.
        // auto_export_minutes is written out too so it's discoverable and editable
        // from the very first run (0 = never auto-start).
        String template = "{\n  \"discord_webhook\": \"\",\n  \"market_id\": \"\",\n  \"market_code\": \"\",\n  \"owner_ign\": \"\",\n  \"auto_export_minutes\": 5,\n  \"brew_aliases\": {}\n}\n";
        try {
            Files.createDirectories(configFile.getParent());
            Files.writeString(configFile, template, StandardCharsets.UTF_8, StandardOpenOption.CREATE_NEW);
        } catch (IOException e) {
            System.err.println("[CSN] Could not write default csn_config.json: " + e.getMessage());
        }
    }

    /** False when this config belongs to a DIFFERENT account than the one playing. */
    static boolean ownerMatches(MinecraftClient client) {
        if (ownerIgn.isEmpty() || marketId.isEmpty()) return true;   // unclaimed / unconfigured
        if (client == null || client.player == null) return true;
        return ownerIgn.equalsIgnoreCase(client.player.getGameProfile().name());
    }

    /** WARN (and continue) when this config belongs to a different account — alts are
     *  normal, so the export is never blocked, but silence here would mean this player's
     *  sales, customers and timestamps get filed under someone else's market unnoticed. */
    static void warnWrongOwner(MinecraftClient client) {
        if (client == null || client.player == null) return;
        String me = client.player.getGameProfile().name();
        client.player.sendMessage(Text.literal(
            "[CSN] Heads up — this sales folder was set up by " + ownerIgn
            + " for market '" + marketId + "', and you are " + me
            + ". Continuing anyway; sales will be filed under '" + marketId + "'."), false);
        client.player.sendMessage(Text.literal(
            "[CSN] If that is NOT your market, open .minecraft/sales/csn_config.json and set "
            + "your own market_id, market_code and discord_webhook, then delete the owner_ign "
            + "line. Ask the bot (@Restocker) for your details, or use /market settings."), false);
    }

    static void saveConfig() {
        ensureConfigDir();
        if (configDir == null) return;
        // Never write the file from statics that were never loaded FROM it — that is
        // exactly the title-screen wipe: blank webhook/id/code/owner + empty aliases
        // truncating a real config. Callers (settings screen) load first.
        if (!configLoaded) {
            System.err.println("[CSN] saveConfig refused: config was never loaded this session");
            return;
        }
        try {
            Files.createDirectories(configDir);
            JsonObject root = new JsonObject();
            root.addProperty("discord_webhook", discordWebhook);
            root.addProperty("market_id",   marketId);
            root.addProperty("market_code", marketCode);
            root.addProperty("owner_ign",   ownerIgn);
            root.addProperty("auto_export_minutes", autoExportMinutes);
            JsonObject aliases = new JsonObject();
            brewAliases.forEach(aliases::addProperty);
            root.add("brew_aliases", aliases);
            Files.writeString(configDir.resolve("csn_config.json"),
                    new Gson().toJson(root), StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
        } catch (IOException e) {
            System.err.println("[CSN] Failed to save config: " + e.getMessage());
        }
    }

    /** Export display name for a sale: resolve profiles/aliases from the RAW name (they
     *  are keyed by the #code — "Diamond Boots#aeV" → "Diamond Boots - Protection IV,
     *  Unbreaking III" via the stock scan's profile); only when nothing matches fall
     *  back to the code-stripped base, so sales and stock names always line up. */
    private String displayNameFor(Entry e) {
        String resolved = resolveAlias(e.itemRaw());
        return resolved.equals(e.itemRaw()) ? e.item() : resolved;
    }

    private String resolveAlias(String rawItem) {
        // 1. Check item profiles — keyed by the raw hash OR the stripped name
        String key = hashToProfileKey.get(rawItem);
        if (key != null) {
            JsonObject profile = itemProfiles.get(key);
            if (profile != null && profile.has("display_name")) {
                String dn = profile.get("display_name").getAsString().trim();
                if (!dn.isEmpty()) return dn;
            }
        }
        // 2. Fall back to legacy brew_aliases in config
        return brewAliases.getOrDefault(rawItem, rawItem);
    }

    private void onReceiveGameMessage(Text message, boolean overlay) {
        if (overlay) return;
        String line = message.getString();

        // Lands tracker sees every line (it only reacts inside its own
        // "/la balance was just sent" window, so this can't misfire).
        LandTracker.onChat(line);

        if (stockScanning) {
            // Some plugins send the whole "Shop Information" block as ONE chat
            // message containing embedded newlines. The line-anchored regexes
            // then match nothing and the shop is silently dropped (captured 0).
            // Split on newlines so each logical line is parsed individually.
            for (String l : line.split("\\r?\\n")) handleStockLine(l);
            return;
        }

        if (!running && !verifyMode) return;

        // Some plugins deliver several logical lines in ONE chat message (the stock path
        // already splits; the export path didn't — a multi-line page read as 0 sales).
        for (String l : line.split("\\r?\\n")) handleExportLine(l);
    }

    private void handleExportLine(String line) {
        if (!seenChatLinesThisRun.add(line)) return;

        long now = System.currentTimeMillis();

        if (verifyMode) {
            Matcher vem = ENTRY_RE.matcher(line);
            if (vem.matches()) {
                verifyGotData = true;
                // Is this a sale we have NOT already exported? Some servers' /csn clear
                // reports success but leaves recent sales listed (the history view is a
                // rolling window, not a clearable log). Retrying the clear against those
                // is futile — and they're harmless, since .seen already filters them.
                // Only UNEXPORTED data means the clear genuinely failed and sales are
                // still at risk.
                if (verifySeen == null) {
                    verifyGotUnseen = true;          // can't tell → assume the worst
                } else if (!isSeen(verifySeen, parseEntry(vem, now))) {
                    verifyGotUnseen = true;
                }
            } else if (PAGE_RE.matcher(line).find()) {
                // A page header during verify means the reply is still streaming in —
                // extend the deadline so a slow (3s-interval) server can't read as
                // "history empty" and get its .seen wiped.
                verifyDeadlineMs = now + VERIFY_RESPONSE_MS;
            }
            return;
        }

        Matcher pm = PAGE_RE.matcher(line);
        if (pm.find()) {
            int newPage = parseOrZero(pm.group(1));
            // Behavioral anchor: another plugin's "Page 1 / 3" used to complete the run
            // early (csn clear then deleted UNREAD pages). Only accept a header that is
            // plausible for OUR outstanding request: the page we asked for, a re-send of
            // the page we're on, or the very first reply of the run.
            if (currentPage > 0 && newPage != pendingPage && newPage != requestedPage
                    && newPage != currentPage) {
                return;   // foreign pagination line — ignore entirely
            }

            lastActivityAtMs = now;   // heartbeat for the stall detector — the server is responding
            pageRetries      = 0;     // a page arrived; the retry budget resets
            boolean repeated = (newPage > 0 && newPage == currentPage);
            currentPage      = newPage;
            int parsedTotal  = parseOrZero(pm.group(2));   // "?" -> 0 (unknown)
            if (parsedTotal > 0) totalPages = parsedTotal;

            // Surface the detected total exactly once, right off the first page, so it's
            // clear the export knows how many pages there are and will fetch them ALL.
            if (totalPages > 0 && !announcedTotal) {
                announcedTotal = true;
                MinecraftClient mcA = MinecraftClient.getInstance();
                if (mcA.player != null) {
                    long etaSec = totalPages * PAGE_REQUEST_DELAY_MS / 1000L;
                    String eta = etaSec >= 60 ? ("~" + ((etaSec + 59) / 60) + " min") : ("~" + etaSec + "s");
                    mcA.player.sendMessage(Text.literal(String.format(
                        "[CSN] Found %d page(s) of history — fetching all of them (%s, no page cap).",
                        totalPages, eta)), false);
                }
            }

            if (totalPages > 0 && currentPage >= totalPages) {
                if (lastPageReceivedAtMs == 0) lastPageReceivedAtMs = now;
            } else if (totalPages > 0) {
                pendingPage    = currentPage + 1;
                nextActionAtMs = now + PAGE_REQUEST_DELAY_MS;
            } else {
                // "Page X / ?" — total unknown. The old code fell through BOTH advance
                // branches (parseInt("?") threw into a silent catch, totalPages stayed 0)
                // and the export deterministically stalled out. Keep walking forward; the
                // server re-showing the SAME page — or a page with no entries since the
                // previous header — marks the end (guards against a server that echoes
                // any requested page number forever).
                boolean emptyPage = currentPage > 1 && entries.size() == entriesAtLastHeader;
                if (repeated || emptyPage) {
                    if (lastPageReceivedAtMs == 0) lastPageReceivedAtMs = now;
                } else {
                    pendingPage    = currentPage + 1;
                    nextActionAtMs = now + PAGE_REQUEST_DELAY_MS;
                }
            }
            entriesAtLastHeader = entries.size();

            MinecraftClient mc = MinecraftClient.getInstance();
            if (mc.player != null && (currentPage % 10 == 0 || currentPage == totalPages)) {
                mc.player.sendMessage(
                    Text.literal(String.format("[CSN] Page %d / %d…", currentPage, totalPages)), true);
            }
            return;
        }

        Matcher em = ENTRY_RE.matcher(line);
        if (!em.matches()) return;
        entries.add(parseEntry(em, now));
    }

    /** Build an Entry from a matched ENTRY_RE line. Shared by the export read and the
     *  post-clear verify, so both judge a sale's identity exactly the same way. */
    private Entry parseEntry(Matcher em, long now) {
        String actor   = em.group(1).trim();
        String verb    = em.group(2).toLowerCase(Locale.ROOT);
        int    qty     = Integer.parseInt(em.group(3));
        String itemRaw = em.group(4).trim();
        String item    = ITEM_CODE_RE.matcher(itemRaw).replaceAll("").trim();

        int days  = parseOrZero(em.group(5));
        int hours = parseOrZero(em.group(6));
        int mins  = parseOrZero(em.group(7));
        int secs  = parseOrZero(em.group(8));

        String sign  = em.group(9);
        double coins = parseCoins(em.group(10));
        if ("-".equals(sign)) coins = -coins;

        long agoMs   = ((long) days * 86_400 + hours * 3_600L + mins * 60L + secs) * 1_000L;
        String tsIso = Instant.ofEpochMilli(now - agoMs).toString();

        return new Entry(actor, sellerName, verb, qty, item, itemRaw, coins, tsIso);
    }

    /** Append entries collected since the last flush to the export CSV, deduped
     *  against the period .seen set. Safe to call repeatedly during a run and once
     *  more at finish(); tracks fresh entries in runFresh for the monthly report.
     *
     *  Ordering matters here: the CSV write happens FIRST, and only on success are the
     *  entries marked seen / counted. The old order marked sales seen even when the write
     *  failed — on a completed run `csn clear` then deleted the server copy and those
     *  sales existed nowhere, permanently. A failed write now just leaves everything to
     *  be retried on the next flush. */
    private void flushCollected() {
        if (runTargets == null || flushedIdx >= entries.size()) return;
        Set<String> seen = loadSeen(runTargets.seenFile());
        if (seen == null) {
            // .seen exists but can't be READ (transient Windows lock / AV scan). Treating
            // that as "empty" used to re-append and re-post everything as fresh, including
            // hive wage lines (double payouts). Skip this flush; the next one retries.
            if (!flushErrorNotified) {
                flushErrorNotified = true;
                MinecraftClient mc = MinecraftClient.getInstance();
                if (mc != null && mc.player != null)
                    mc.player.sendMessage(Text.literal(
                        "[CSN] Couldn't read the dedup (.seen) file — holding this flush and retrying. "
                        + "Nothing is lost."), false);
            }
            return;
        }
        boolean newFile = !Files.exists(runTargets.dataFile(), LinkOption.NOFOLLOW_LINKS);
        StringBuilder sb = new StringBuilder();
        boolean wroteMarketHeader = false;
        if (newFile) {
            sb.append("# PERIOD,").append(periodFrom()).append(',').append(periodTo()).append('\n');
            if (!marketId.isEmpty()) {
                sb.append("# MARKET,").append(csvField(marketId)).append(',')
                  .append(csvField(marketCode)).append('\n');
                wroteMarketHeader = true;
            }
            sb.append("actor,seller,verb,quantity,item,amount_coins,timestamp_iso,sale_uid\n");
        } else if (!marketHeaderWrittenThisRun && !marketId.isEmpty()) {
            // Re-emit the MARKET header once per run even on an existing file: it used to
            // be written only when the file was NEW, so a mid-month config change or code
            // rotation broke attribution until the file rolled over. (The bot reads the
            // LAST # MARKET line, so the newest config wins.)
            sb.append("# MARKET,").append(csvField(marketId)).append(',')
              .append(csvField(marketCode)).append('\n');
            wroteMarketHeader = true;
        }

        int idx = flushedIdx;
        Set<String> added = new LinkedHashSet<>();
        List<Entry> batch = new ArrayList<>();
        double batchProfit = 0, batchLoss = 0;
        while (idx < entries.size()) {
            Entry e = entries.get(idx++);
            String uid = saleUid(e);
            if (isSeen(seen, e) || added.contains(uid)) continue;
            added.add(uid);
            String displayItem = displayNameFor(e);
            sb.append(csvField(e.actor())).append(',')
              .append(csvField(e.seller())).append(',')
              .append(e.verb()).append(',')
              .append(e.quantity()).append(',')
              .append(csvField(displayItem)).append(',')
              .append(fmt(e.amountCoins())).append(',')
              .append(e.timestampIso()).append(',')
              .append(uid).append('\n');
            batch.add(e);
            if (e.amountCoins() >= 0) batchProfit += e.amountCoins();
            else                      batchLoss   += e.amountCoins();
        }

        if (batch.isEmpty() && !newFile && !wroteMarketHeader) {
            flushedIdx = idx;    // everything was a duplicate — nothing to write
            return;
        }
        try {
            Files.writeString(runTargets.dataFile(), sb.toString(), StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        } catch (IOException ex) {
            ex.printStackTrace();
            if (!flushErrorNotified) {
                flushErrorNotified = true;
                MinecraftClient mc = MinecraftClient.getInstance();
                if (mc != null && mc.player != null)
                    mc.player.sendMessage(Text.literal(
                        "[CSN] Couldn't write the export CSV (" + ex.getMessage()
                        + ") — will retry; the collected sales are NOT marked exported."), false);
            }
            return;   // nothing marked seen, flushedIdx unchanged — the next flush retries
        }

        // Write succeeded — only now do the entries count as exported.
        flushedIdx = idx;
        if (wroteMarketHeader) marketHeaderWrittenThisRun = true;
        runFresh.addAll(batch);
        runProfit += batchProfit;
        runLoss   += batchLoss;
        if (!added.isEmpty()) {
            seen.addAll(added);
            saveSeen(runTargets.seenFile(), seen);
        }
    }

    private void finish(MinecraftClient client, String reason) {
        running = false;

        if (runTargets == null) runTargets = resolveTargets(client);
        ExportTargets targets = runTargets;

        // Entries are flushed to disk incrementally during the run (crash-safe), so
        // this just flushes the final batch, appends the single run-summary footer,
        // and writes the monthly report from this run's fresh entries. De-dup is done
        // per-flush against the period .seen set, so monthly totals stay correct even
        // when history is re-read (e.g. after an interrupted run or a failed clear).
        flushCollected();
        // Only stamp a RUN block when the run actually achieved something. A fruitless
        // run (stalled before any page arrived) used to append "parsed=0,pages=0" every
        // time, so the period file filled with empty blocks that were then re-uploaded
        // to Discord on every retry.
        if (!runFresh.isEmpty() || totalPages > 0) {
            StringBuilder _sum = new StringBuilder();
            _sum.append("# RUN,").append(runTimestampIso)
                .append(",parsed=").append(runFresh.size())
                .append(",pages=").append(totalPages).append('\n');
            _sum.append("# RUN_SUMMARY,profit=").append(fmt(runProfit))
                .append(",loss=").append(fmt(runLoss))
                .append(",net=").append(fmt(runProfit + runLoss)).append('\n');
            try {
                Files.writeString(targets.dataFile(), _sum.toString(), StandardCharsets.UTF_8,
                        StandardOpenOption.CREATE, StandardOpenOption.APPEND);
            } catch (IOException _e) {
                _e.printStackTrace();
            }
        }
        List<Path> monthlyFiles = writeMonthlyReport(targets, runFresh);
        int newCount = runFresh.size();

        // Every CSN run doubles as a lands checkpoint (teleport-fee inference feeds
        // on balance snapshots over time).
        LandTracker.autoSweep(client);

        boolean completed = "complete".equals(reason);

        String msg = String.format(
                "[CSN] Export %s — %d new entries written. File saved to .minecraft/sales/",
                reason, newCount);
        if (client.player != null) {
            client.player.sendMessage(Text.literal(msg), false);
            // ONLY clear history when the whole thing was read. On an early end
            // (stalled / backstop) the un-fetched pages are still in the plugin, so
            // clearing would delete sales that were never exported. Keep them instead;
            // a re-run picks up where this left off (the .seen set prevents double-count).
            if (completed) {
                sendCommand(client, "csn clear");
            } else {
                client.player.sendMessage(Text.literal(
                    "[CSN] Export ended early (" + reason + ") — history was NOT cleared, so no sales "
                    + "are lost. Just run the export again to capture the rest."), false);
            }
        }

        if (!discordWebhook.isEmpty()) {
            // The PERIOD file is the per-transaction ledger the monthly report is
            // aggregated FROM: actor,seller,verb,quantity,item,amount_coins,timestamp_iso.
            // It was written to disk but never sent, so who bought what — and when — died
            // on this machine. Ship both: monthly for the headline numbers, period for
            // customer and time-of-sale analysis.
            Path periodFile  = targets.dataFile();
            // A run that spans a month boundary produces MULTIPLE monthly files — ship all.
            List<Path> bundleFiles = new ArrayList<>(monthlyFiles.isEmpty()
                    ? List.of(targets.monthlyFile()) : monthlyFiles);
            bundleFiles.add(periodFile);
            String webhook   = discordWebhook;
            // ONE consolidated message. The bot DELETES this whole message once it has
            // ingested both files and posted its own report card, so this text is really
            // only read when something went wrong. Write it for that case: plain English,
            // and say what to do next instead of dumping raw counters.
            String label = marketId.isEmpty() ? "CSN" : marketId;
            String tail = completed ? "" :
                    " \u00B7 \u26A0\uFE0F scan ended early (" + reason + ") \u2014 press F6 again to read"
                    + " the rest. Nothing is lost: history is only cleared after a full read.";
            String summary = (newCount == 0)
                    ? "\uD83D\uDCCA **" + label + "** \u00B7 no new sales since the last scan" + tail
                    : "\uD83D\uDCCA **" + label + "** \u00B7 " + newCount + " new sale"
                      + (newCount == 1 ? "" : "s")
                      + " \u00B7 earned " + fmt(runProfit)
                      + " \u00B7 spent " + fmt(-runLoss)
                      + " \u00B7 net " + fmt(runProfit + runLoss) + tail;
            // ONE virtual thread, posts SERIALIZED. Firing csn-push and csn-hive
            // concurrently invited Discord's per-webhook rate limit, and a 429 on the
            // hive post silently un-paid harvesters. Sequential posts (each honouring
            // Retry-After inside) can't race each other into the limit; the hive post
            // also spools unsent lines to disk until a 2xx.
            // Per-seller hive wages: post THIS run's fresh honey/comb "sold" lines so the
            // Restocker bot's hive autopay pays each harvester by IGN. Snapshot runFresh
            // (the .seen-deduped set) so each sale is posted — and paid — exactly once.
            List<Entry> harvestFresh = new ArrayList<>(runFresh);
            // NOTHING NEW -> POST NOTHING. Re-uploading an unchanged CSV taught the bot
            // nothing and cost a channel message every time; with several alts exporting
            // on a loop it buried the channel, and each re-ingest of a stale monthly file
            // re-wrote that market's month. The player still sees the result in chat.
            // postHiveHarvest still runs — it drains the wage spool, which is how a
            // rate-limited payout from an earlier run finally gets delivered.
            boolean postFiles = newCount > 0;
            Thread.ofVirtual().name("csn-push").start(() -> {
                if (postFiles) postBundle(webhook, summary, bundleFiles, client);
                postHiveHarvest(webhook, harvestFresh, client);
            });
        }

        // Verify the clear actually emptied the plugin history; retry if not. On a
        // confirmed-empty result, reset the period .seen so repeated identical sales
        // in the next cycle are counted exactly instead of dropped as duplicates.
        lastTargets   = targets;
        // Only verify the clear when we actually cleared (a completed run). On an early
        // end we deliberately kept the history, so there's nothing to verify or reset —
        // the .seen set stays intact so the next run de-dups what was already written.
        if (client.player != null && completed) {
            clearAttempts = 1;   // the clear sent just above is attempt #1
            beginVerify();
        }
    }

    // ── live shop-stock scan ─────────────────────────────────────────────────
    private void startStockScan(MinecraftClient client) {
        stockScanning = true;
        stockShops.clear();
        stockLastCaptureMs = 0;
        resetShopBlock();
        if (client.player != null) {
            client.player.sendMessage(Text.literal(
                "[CSN] Stock scan ON - click each of your shops. Saves automatically "
                + (STOCK_IDLE_SAVE_MS / 1000) + "s after the last one (or press the key again)."), false);
        }
        warnMarketConfig(client);
    }

    private void stopStockScan(MinecraftClient client) {
        flushShopBlock();
        stockScanning = false;
        int n = stockShops.size();
        boolean saved = writeStockReport(client);
        if (client.player != null) {
            // Never claim success when the CSV write failed — the scan data would be
            // gone and the player told everything was fine.
            client.player.sendMessage(Text.literal(saved
                ? String.format("[CSN] Stock scan OFF - captured %d barrel(s), saved csn_stock_*.csv%s",
                    n, discordWebhook.isEmpty() ? "" : " (posted to Discord)")
                : String.format("[CSN] Stock scan OFF - captured %d barrel(s) but SAVING FAILED — "
                    + "check disk space/permissions on .minecraft/sales/ and scan again.", n)), false);
        }
    }

    private void handleStockLine(String line) {
        if (SHOP_HEADER_RE.matcher(line).matches()) {
            flushShopBlock();
            startShopBlock();
            return;
        }
        if (!inShopBlock) return;

        Matcher m;
        if ((m = SHOP_OWNER_RE.matcher(line)).matches()) { pOwner = m.group(1).trim(); return; }
        if ((m = SHOP_STOCK_RE.matcher(line)).matches()) {
            try { pStock = Long.parseLong(m.group(1).replace(",", "")); }
            catch (NumberFormatException e) {
                System.err.println("[CSN] Unparseable Stock: line — " + line);
            }
            return;
        }
        if ((m = SHOP_ITEM_RE.matcher(line)).matches()) {
            pItemRaw = m.group(1).trim();
            // Strip color/variant suffix (#afY, #aid, #aFe …) — same rule as the sales path
            pItem    = ITEM_CODE_RE.matcher(pItemRaw).replaceAll("").trim();
            return;
        }
        if ((m = SHOP_PRICE_RE.matcher(line)).matches()) {
            String prefix = m.group(1);
            int qty = parseOrZero(m.group(2));
            double price = parseCoins(m.group(3));
            boolean isBuy = (prefix != null) ? prefix.equalsIgnoreCase("Buy") : (pPriceLineCount == 0);
            if (isBuy) { pBuyQty = qty; pBuyPrice = price; }
            else       { pSellQty = qty; pSellPrice = price; }
            pPriceLineCount++;
            if (pPriceLineCount >= 2) flushShopBlock();
            return;
        }
        // Lore / enchant / description lines — capture them (skip the "Price Cost:" UI label).
        // Server broadcasts interleave with the block dump — "[Shop] You can't sell here!"
        // arrives the instant a sell-only shop is clicked and used to pollute the lore (and
        // via deriveDisplayName, the item's display name). Filter: lore only counts once the
        // Item: line was seen, and bracketed/server-chat lines are never lore.
        String trimmed = line.trim();
        if (trimmed.isEmpty() || pItem == null) return;
        // Lore/enchant lines sit BETWEEN "Item:" and the price lines. Once a price line
        // has arrived, anything further is interleaved server chat ("Alex joined the
        // game" used to become lore AND — via translateEnchants — rename the item).
        if (pPriceLineCount > 0) return;
        if (trimmed.equalsIgnoreCase("Price Cost:")) return;
        if (trimmed.startsWith("[") || trimmed.startsWith("DC ") || trimmed.startsWith("DC>")) return;
        if (SERVER_NOISE_RE.matcher(trimmed).find()) return;
        pLore.add(trimmed);
    }

    private void startShopBlock() {
        inShopBlock = true;
        resetShopBlockFields();
        // Identify the physical barrel: the Shop Information block arrives right
        // after the click, so the crosshair is still on the barrel. Keying captures
        // by block position lets MULTIPLE barrels of the same item count separately
        // (summed at save time), while re-clicking the same barrel just overwrites.
        try {
            MinecraftClient mc = MinecraftClient.getInstance();
            if (mc != null && mc.crosshairTarget != null
                    && mc.crosshairTarget.getType() == HitResult.Type.BLOCK) {
                pPos = ((BlockHitResult) mc.crosshairTarget).getBlockPos().toShortString();
            }
        } catch (Exception ignored) {}
    }

    private void resetShopBlock() {
        inShopBlock = false;
        resetShopBlockFields();
    }

    private void resetShopBlockFields() {
        pOwner = null; pItem = null; pItemRaw = null; pStock = -1;
        pBuyQty = 0; pSellQty = 0; pBuyPrice = -1; pSellPrice = -1;
        pPriceLineCount = 0; pLore = new ArrayList<>(); pPos = null;
    }

    private void flushShopBlock() {
        if (inShopBlock && pItem != null && pStock >= 0) {
            List<String> loreCopy = List.copyOf(pLore);
            // Name enchanted tools by their enchants; otherwise apply the alias/profile name.
            String enchName = deriveDisplayName(pItem, loreCopy);
            String display  = enchName.equals(pItem) ? resolveAlias(pItem) : enchName;
            // Key by the barrel's block position when we have it: each physical
            // barrel is its own entry (summed per owner|item at save time), and
            // re-clicking the same barrel overwrites instead of double-counting.
            // Fallback (no position): old owner|item behavior — last click wins.
            String key = (pPos != null)
                    ? "pos:" + pPos
                    : (pOwner == null ? "" : pOwner) + "|" + display;
            StockShop shop = new StockShop(
                pOwner == null ? "" : pOwner,
                display,
                pItemRaw != null ? pItemRaw : pItem,
                pStock,
                pBuyQty, pBuyPrice, pSellQty, pSellPrice,
                loreCopy, Instant.now().toString());
            stockShops.put(key, shop);
            upsertProfile(shop);
            stockLastCaptureMs = System.currentTimeMillis();   // restart the auto-save idle timer
            // Immediate action-bar feedback so it's obvious the shop was captured
            MinecraftClient mc = MinecraftClient.getInstance();
            if (mc != null && mc.player != null) {
                mc.player.sendMessage(Text.literal(String.format(
                    "[CSN] Captured %s (stock %d) — %d barrel(s), saving in %ds",
                    display, pStock, stockShops.size(), STOCK_IDLE_SAVE_MS / 1000)), true);
            }
        }
        resetShopBlock();
    }

    private boolean writeStockReport(MinecraftClient client) {
        if (configDir == null) configDir = client.runDirectory.toPath().resolve("sales");
        try { Files.createDirectories(configDir); } catch (IOException ignored) {}
        String ts = Instant.now().toString().replace(":", "-").replace(".", "-");
        Path stockFile = configDir.resolve("csn_stock_" + ts + ".csv");

        StringBuilder sb = new StringBuilder();
        sb.append("# STOCK_REPORT,").append(stockFile.getFileName()).append('\n');
        if (!marketId.isEmpty()) {
            sb.append("# MARKET,").append(csvField(marketId)).append(',')
              .append(csvField(marketCode)).append('\n');
        }
        // raw_item (APPENDED for DictReader back-compat) carries the item's ORIGINAL
        // name including its #code. The display name has the code stripped, so the bot's
        // stock-scan alias learning ("#" in raw_item) matched nothing, ever — the whole
        // learn-from-stock path was dead code until this column existed.
        sb.append("owner,item,stock,buy_qty,buy_price,sell_qty,sell_price,lore,timestamp_iso,barrels,raw_item\n");
        // Aggregate the per-barrel captures: SUM stock across an owner's barrels of
        // the same item (10 andesite barrels = total of all 10). Prices/lore/timestamp
        // come from the most recently captured barrel of that item.
        Map<String, StockShop> agg = new LinkedHashMap<>();
        Map<String, Long> aggStock = new LinkedHashMap<>();
        Map<String, Integer> aggBarrels = new LinkedHashMap<>();
        for (StockShop sh : stockShops.values()) {
            String k = sh.owner() + "|" + sh.item();
            agg.put(k, sh);                                   // latest capture wins for prices/lore/ts
            aggStock.merge(k, sh.stock(), Long::sum);         // stock sums across barrels
            aggBarrels.merge(k, 1, Integer::sum);
        }
        for (Map.Entry<String, StockShop> e : agg.entrySet()) {
            StockShop sh = e.getValue();
            String loreField = String.join(" | ", sh.lore());
            sb.append(csvField(sh.owner())).append(',')
              .append(csvField(sh.item())).append(',')
              .append(aggStock.get(e.getKey())).append(',')
              .append(sh.buyQty()).append(',')
              .append(sh.buyPrice() < 0 ? "" : fmt(sh.buyPrice())).append(',')
              .append(sh.sellQty()).append(',')
              .append(sh.sellPrice() < 0 ? "" : fmt(sh.sellPrice())).append(',')
              .append(csvField(loreField)).append(',')
              .append(sh.tsIso()).append(',')
              .append(aggBarrels.get(e.getKey())).append(',')
              .append(csvField(sh.itemRaw())).append('\n');
        }
        try {
            Files.writeString(stockFile, sb.toString(), StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
        } catch (IOException e) {
            e.printStackTrace();
            saveProfiles();   // profiles are independent of the CSV — keep what we learned
            return false;
        }
        saveProfiles();
        Path profilesFile = configDir.resolve("csn_profiles.json");
        if (!discordWebhook.isEmpty()) {
            if (agg.isEmpty()) {
                notify(client, "[CSN] Stock scan captured 0 shops — nothing posted.");
                return true;
            }
            String webhook = discordWebhook;
            List<Path> bundle = new ArrayList<>();
            bundle.add(stockFile);
            // Attach profiles ONLY when its content changed since the last post — it grows to
            // hundreds of KB and re-sending it with every scan was pure channel spam. A
            // sidecar hash file remembers what was last posted.
            try {
                if (Files.exists(profilesFile) && Files.size(profilesFile) > 4L) {
                    byte[] pb = Files.readAllBytes(profilesFile);
                    String h = java.util.HexFormat.of().formatHex(
                            java.security.MessageDigest.getInstance("SHA-256").digest(pb));
                    Path hashFile = profilesFile.resolveSibling("csn_profiles.posted_hash");
                    String last = Files.exists(hashFile)
                            ? Files.readString(hashFile, StandardCharsets.UTF_8).trim() : "";
                    if (!h.equals(last)) {
                        bundle.add(profilesFile);
                        Files.writeString(hashFile, h, StandardCharsets.UTF_8);
                    }
                }
            } catch (Exception ignored) {}
            String summary = "\uD83E\uDDFA **" + (marketId.isEmpty() ? "CSN" : marketId)
                    + " stock scan** — " + agg.size() + " shop(s), "
                    + stockShops.size() + " barrel(s) captured";
            Thread.ofVirtual().name("csn-stock-push").start(() ->
                postBundle(webhook, summary, bundle, client));
        }
        return true;
    }

    private void beginVerify() {
        verifyMode      = true;
        verifyRequested = false;
        verifyGotData   = false;
        verifyGotUnseen = false;
        // Snapshot .seen so the verify read can tell "already exported" from "at risk".
        verifySeen      = (lastTargets != null) ? loadSeen(lastTargets.seenFile()) : null;
        currentPage     = 0;
        totalPages      = 0;
        seenChatLinesThisRun.clear();
        // Back off between clear attempts: a big backlog can take longer than 2s to
        // clear, and re-reading too early reads as "clear failed" when it just hadn't
        // finished. 2s, then 6s, then 12s.
        long delay = VERIFY_DELAY_MS * Math.max(1, clearAttempts * clearAttempts);
        nextActionAtMs  = System.currentTimeMillis() + Math.min(delay, 15_000L);
    }

    private void tickVerify(MinecraftClient client, long now) {
        if (!verifyRequested) {
            if (now >= nextActionAtMs) {
                sendCommand(client, "csn history 1");
                verifyRequested  = true;
                verifyDeadlineMs = now + VERIFY_RESPONSE_MS;
            }
            return;
        }
        if (now >= verifyDeadlineMs) {
            concludeVerify(client);
        }
    }

    private void concludeVerify(MinecraftClient client) {
        verifyMode = false;
        if (!verifyGotData) {
            if (lastTargets != null) resetSeen(lastTargets.seenFile());
            if (client.player != null) {
                client.player.sendMessage(
                    Text.literal("[CSN] /csn clear verified - history empty, dedup reset."), false);
            }
            return;
        }
        // The history still lists sales, but every one of them is ALREADY exported.
        // On servers whose history view is a rolling window, /csn clear reports success
        // and the recent sales stay listed forever — re-sending the clear can never
        // change that, and nothing is at risk because .seen filters them. Say so once
        // and stop, instead of burning three attempts and ending on a scary warning.
        if (!verifyGotUnseen) {
            if (client.player != null) {
                client.player.sendMessage(Text.literal(
                    "[CSN] Clear done - the history view still lists recent sales, but all of them "
                    + "are already exported, so nothing is missing. (This server keeps recent sales "
                    + "visible after a clear; they're filtered as duplicates.)"), false);
            }
            return;   // .seen deliberately NOT reset: those sales are still on screen.
        }
        if (clearAttempts < MAX_CLEAR_ATTEMPTS) {
            clearAttempts++;
            if (client.player != null) {
                client.player.sendMessage(Text.literal(String.format(
                    "[CSN] History still has unexported sales - re-sending /csn clear (attempt %d/%d)...",
                    clearAttempts, MAX_CLEAR_ATTEMPTS)), false);
                sendCommand(client, "csn clear");
            }
            beginVerify();
        } else if (client.player != null) {
            client.player.sendMessage(Text.literal(String.format(
                "[CSN] WARNING: /csn clear did not empty history after %d tries - clear it manually. "
                + "Totals stay correct (duplicates are filtered).", MAX_CLEAR_ATTEMPTS)), false);
        }
    }

    private static void resetSeen(Path seenFile) {
        try {
            Files.writeString(seenFile, "", StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
        } catch (IOException e) {
            // A failed reset means the next period keeps deduping against stale hashes —
            // repeated identical sales would be dropped. Say so instead of hiding it.
            System.err.println("[CSN] Could not reset .seen: " + e.getMessage());
        }
    }

    private static LocalDate periodStartDate() {
        LocalDate today = LocalDate.now();
        if (today.getDayOfMonth() >= PERIOD_START_DAY)
            return today.withDayOfMonth(PERIOD_START_DAY);
        LocalDate prev = today.minusMonths(1);
        return prev.withDayOfMonth(Math.min(PERIOD_START_DAY, prev.lengthOfMonth()));
    }

    /** The export period's first / last day (ISO dates) — written to the # PERIOD header.
     *  The header used to carry only the FILENAME, but the bot's parser expects
     *  `# PERIOD,<from>,<to>`, so period_from/to always parsed as None. */
    private static String periodFrom() {
        return periodStartDate().format(DateTimeFormatter.ISO_LOCAL_DATE);
    }

    private static String periodTo() {
        return periodStartDate().plusMonths(1).minusDays(1).format(DateTimeFormatter.ISO_LOCAL_DATE);
    }

    private ExportTargets resolveTargets(MinecraftClient client) {
        LocalDate today = LocalDate.now();
        LocalDate periodStart = periodStartDate();

        if (configDir == null) configDir = client.runDirectory.toPath().resolve("sales");
        try { Files.createDirectories(configDir); } catch (IOException ignored) {}

        String period = periodStart.format(DateTimeFormatter.ISO_LOCAL_DATE);
        String month  = today.getYear() + "-" + String.format("%02d", today.getMonthValue());

        return new ExportTargets(
                configDir.resolve("csn_export_" + period + ".csv"),
                configDir.resolve("csn_export_" + period + ".seen"),
                configDir.resolve("csn_monthly_" + month + ".csv")
        );
    }

    /** Write this run's fresh entries into monthly report file(s) and return the files
     *  written (for the webhook bundle).
     *
     *  Entries are attributed to the month of their SALE TIMESTAMP, not "now": a 35-day
     *  history read used to file last month's sales into the current month while each
     *  row's own date said otherwise. A run spanning a month boundary now updates BOTH
     *  monthly files, each with only its own month's sales.
     *
     *  Every run stamps `# MODE,delta` so the bot KNOWS each RUN block holds only this
     *  run's fresh entries and sums the blocks — instead of guessing cumulative-vs-delta
     *  from monotonicity and discarding 33–50%% of real earnings when it guessed wrong. */
    private List<Path> writeMonthlyReport(ExportTargets targets, List<Entry> newEntries) {
        if (newEntries.isEmpty()) return List.of();

        // Group by the sale's own month (fallback: the current month if the ts is odd)
        String fallbackMonth = targets.monthlyFile().getFileName().toString()
                .replace("csn_monthly_", "").replace(".csv", "");
        Map<String, List<Entry>> byMonth = new LinkedHashMap<>();
        for (Entry e : newEntries) {
            String ts = e.timestampIso();
            String month = (ts != null && ts.length() >= 7) ? ts.substring(0, 7) : fallbackMonth;
            byMonth.computeIfAbsent(month, k -> new ArrayList<>()).add(e);
        }

        List<Path> written = new ArrayList<>();
        Path dir = targets.monthlyFile().getParent();
        for (Map.Entry<String, List<Entry>> me : byMonth.entrySet()) {
            Path monthlyFile = dir.resolve("csn_monthly_" + me.getKey() + ".csv");

            Map<String, double[]> stats = new LinkedHashMap<>();
            for (Entry e : me.getValue()) {
                String displayItem = displayNameFor(e);
                double[] s = stats.computeIfAbsent(displayItem, k -> new double[6]);
                if ("bought".equals(e.verb())) {
                    s[0] += e.quantity();
                    s[2] += e.amountCoins();
                    s[4]++;
                } else {
                    s[1] += e.quantity();
                    s[3] += e.amountCoins();
                    s[5]++;
                }
            }

            boolean newFile = !Files.exists(monthlyFile, LinkOption.NOFOLLOW_LINKS);
            StringBuilder sb = new StringBuilder();

            if (newFile) {
                sb.append("# MONTHLY_REPORT,").append(monthlyFile.getFileName()).append('\n');
                // income_coins/expense_coins added v1.2: income = revenue from your sales (verb
                // "bought", ≥0), expense = coins you spent buying (verb "sold", ≤0), and the identity
                // net_coins = income_coins + expense_coins always holds. Columns are APPENDED so the
                // bot's name-based (DictReader) parser stays backward-compatible with old files.
                sb.append("item,total_sold_qty,total_bought_qty,net_coins,times_sold,times_bought,"
                        + "income_coins,expense_coins\n");
            }

            // Re-emitted EVERY run (not just on a new file): the bot honours the LAST
            // # MARKET line, so a mid-month config change or code rotation takes effect
            // immediately, and `# MODE,delta` tells the parser these blocks are per-run
            // deltas even when the file was started by an older build.
            if (!marketId.isEmpty()) {
                sb.append("# MARKET,").append(csvField(marketId)).append(',')
                  .append(csvField(marketCode)).append('\n');
            }
            sb.append("# MODE,delta\n");
            sb.append("# RUN,").append(runTimestampIso).append('\n');

            for (Map.Entry<String, double[]> kv : stats.entrySet()) {
                double[] s = kv.getValue();
                sb.append(csvField(kv.getKey())).append(',')
                  .append((long) s[0]).append(',')
                  .append((long) s[1]).append(',')
                  .append(fmt(s[2] + s[3])).append(',')
                  .append((long) s[4]).append(',')
                  .append((long) s[5]).append(',')
                  .append(fmt(s[2])).append(',')
                  .append(fmt(s[3])).append('\n');
            }

            try {
                Files.writeString(monthlyFile, sb.toString(), StandardCharsets.UTF_8,
                        StandardOpenOption.CREATE, StandardOpenOption.APPEND);
                written.add(monthlyFile);
            } catch (IOException e) {
                e.printStackTrace();
            }
        }
        return written;
    }

    // ── item profile database ─────────────────────────────────────────────

    /** profileKey = baseItemName@sellPrice (fallback to buyPrice if no sell price). */
    private static String profileKey(String baseItem, double buyPrice, double sellPrice) {
        double anchor = (sellPrice >= 0) ? sellPrice : buyPrice;
        if (anchor < 0) return baseItem;
        return baseItem + "@" + (long) Math.round(anchor);
    }

    // ── Enchanted-tool naming ────────────────────────────────────────────────
    // A shop's "Item:" line is a bare/ambiguous name (e.g. Diamond Pickaxe#afx); the
    // tool's real identity is the enchant lines under it. We translate those old-style
    // labels to common enchant names and append them, so variants are distinguishable
    // and the stock name lines up with the sales name (via the profile code→name map).
    private static final Map<String, String> ENCHANT_NAMES = new HashMap<>();
    static {
        ENCHANT_NAMES.put("dig speed", "Efficiency");
        ENCHANT_NAMES.put("durability", "Unbreaking");
        ENCHANT_NAMES.put("loot bonus blocks", "Fortune");
        ENCHANT_NAMES.put("loot bonus mobs", "Looting");
        ENCHANT_NAMES.put("damage", "Sharpness");
        ENCHANT_NAMES.put("fire aspect", "Fire Aspect");
        ENCHANT_NAMES.put("silk touch", "Silk Touch");
        ENCHANT_NAMES.put("mending", "Mending");
        ENCHANT_NAMES.put("knockback", "Knockback");
        ENCHANT_NAMES.put("protection", "Protection");
        ENCHANT_NAMES.put("fire protection", "Fire Protection");
        ENCHANT_NAMES.put("blast protection", "Blast Protection");
        ENCHANT_NAMES.put("projectile protection", "Projectile Protection");
        ENCHANT_NAMES.put("feather falling", "Feather Falling");
        ENCHANT_NAMES.put("thorns", "Thorns");
        ENCHANT_NAMES.put("respiration", "Respiration");
        ENCHANT_NAMES.put("aqua affinity", "Aqua Affinity");
        ENCHANT_NAMES.put("depth strider", "Depth Strider");
        ENCHANT_NAMES.put("power", "Power");
        ENCHANT_NAMES.put("punch", "Punch");
        ENCHANT_NAMES.put("flame", "Flame");
        ENCHANT_NAMES.put("infinity", "Infinity");
        ENCHANT_NAMES.put("luck of the sea", "Luck of the Sea");
        ENCHANT_NAMES.put("lure", "Lure");
        ENCHANT_NAMES.put("sweeping", "Sweeping Edge");
        ENCHANT_NAMES.put("sweeping edge", "Sweeping Edge");
        // Legacy Bukkit enchant names (the server's item lore uses these on books/gear)
        ENCHANT_NAMES.put("damage all", "Sharpness");
        ENCHANT_NAMES.put("damage undead", "Smite");
        ENCHANT_NAMES.put("damage arthropods", "Bane of Arthropods");
        ENCHANT_NAMES.put("arrow damage", "Power");
        ENCHANT_NAMES.put("arrow fire", "Flame");
        ENCHANT_NAMES.put("arrow knockback", "Punch");
        ENCHANT_NAMES.put("arrow infinite", "Infinity");
        ENCHANT_NAMES.put("protection environmental", "Protection");
        ENCHANT_NAMES.put("protection fire", "Fire Protection");
        ENCHANT_NAMES.put("protection explosions", "Blast Protection");
        ENCHANT_NAMES.put("protection projectile", "Projectile Protection");
        ENCHANT_NAMES.put("protection fall", "Feather Falling");
        ENCHANT_NAMES.put("oxygen", "Respiration");
        ENCHANT_NAMES.put("water worker", "Aqua Affinity");
        ENCHANT_NAMES.put("luck", "Luck of the Sea");
        ENCHANT_NAMES.put("frost walker", "Frost Walker");
        ENCHANT_NAMES.put("binding curse", "Curse of Binding");
        ENCHANT_NAMES.put("vanishing curse", "Curse of Vanishing");
        ENCHANT_NAMES.put("soul speed", "Soul Speed");
        ENCHANT_NAMES.put("swift sneak", "Swift Sneak");
        ENCHANT_NAMES.put("quick charge", "Quick Charge");
        ENCHANT_NAMES.put("multishot", "Multishot");
        ENCHANT_NAMES.put("piercing", "Piercing");
        ENCHANT_NAMES.put("loyalty", "Loyalty");
        ENCHANT_NAMES.put("channeling", "Channeling");
        ENCHANT_NAMES.put("riptide", "Riptide");
        ENCHANT_NAMES.put("impaling", "Impaling");
    }

    private static final Pattern ENCH_LEVEL_RE = Pattern.compile("^(.*?)\\s+([IVXLC]+|\\d+)$");

    /** Canonical enchant names (the map's values), lowercased — so a lore line that is
     *  ALREADY canonical ("Efficiency V") passes the whitelist below. */
    private static final Set<String> ENCHANT_CANON = new HashSet<>();
    static {
        for (String v : ENCHANT_NAMES.values()) ENCHANT_CANON.add(v.toLowerCase(Locale.ROOT));
    }

    /** Turn raw enchant lore lines into common "Name Level" strings.
     *
     *  WHITELIST: only lines whose name resolves through the enchant map (or is already a
     *  canonical enchant name) count. The old version kept ANY line without a colon, so a
     *  brew's star bar, "Barrel aged", flavour prose — even interleaved server chat —
     *  became "enchants" and were baked into the item's display name ("Potion -
     *  §s§8[⭑⭑⭑⭑⭑], Barrel aged" in the live DB is exactly this). Brews now keep their
     *  clean base name and get their REAL effects from the alias/profile pipeline. */
    private static List<String> translateEnchants(List<String> lore) {
        List<String> out = new ArrayList<>();
        for (String raw : lore) {
            String s = raw.trim();
            if (s.isEmpty() || s.contains(":")) continue;
            String name = s, level = "";
            Matcher m = ENCH_LEVEL_RE.matcher(s);
            if (m.matches()) { name = m.group(1).trim(); level = m.group(2); }
            // normalize: underscores → spaces so "DAMAGE_ALL" and "Damage All" both resolve
            String norm = name.toLowerCase(Locale.ROOT).replace('_', ' ').trim();
            String common;
            if (ENCHANT_NAMES.containsKey(norm)) {
                common = ENCHANT_NAMES.get(norm);
            } else if (ENCHANT_CANON.contains(norm)) {
                common = name;
            } else {
                continue;   // not an enchant-shaped line — never bake it into a name
            }
            out.add(level.isEmpty() ? common : common + " " + level);
        }
        return out;
    }

    /** Base item + its enchants → readable name, e.g. "Diamond Pickaxe" +
     *  [Dig Speed V, Durability III, Loot Bonus Blocks III] ->
     *  "Diamond Pickaxe - Efficiency V, Unbreaking III, Fortune III". Base unchanged if no enchants. */
    private static String deriveDisplayName(String base, List<String> lore) {
        List<String> e = translateEnchants(lore);
        e.sort(null);   // deterministic canonical order (alphabetical) → CSN name always matches the shop catalog
        return e.isEmpty() ? base : base + " - " + String.join(", ", e);
    }

    /**
     * Create or update the item profile for a just-scanned barrel.
     * Preserves any display_name and effects the user has already filled in.
     */
    private static void upsertProfile(StockShop sh) {
        if (sh.item() == null || sh.item().isEmpty()) return;
        String key = profileKey(sh.item(), sh.buyPrice(), sh.sellPrice());

        JsonObject profile = itemProfiles.computeIfAbsent(key, k -> {
            JsonObject p = new JsonObject();
            p.addProperty("display_name", "");   // user fills this in
            p.add("effects", new com.google.gson.JsonArray());
            return p;
        });

        // Always refresh prices and lore from the latest scan
        profile.addProperty("buy_price",  sh.buyPrice() < 0  ? 0 : sh.buyPrice());
        profile.addProperty("sell_price", sh.sellPrice() < 0 ? 0 : sh.sellPrice());

        com.google.gson.JsonArray loreArr = new com.google.gson.JsonArray();
        sh.lore().forEach(loreArr::add);
        profile.add("lore", loreArr);

        // Auto-fill a readable display name (enchants/alias) if the user hasn't set one,
        // and store the translated enchants as effects so the profile self-documents.
        String dn = (profile.has("display_name") && !profile.get("display_name").isJsonNull())
                ? profile.get("display_name").getAsString().trim() : "";
        // SELF-HEAL: names auto-filled by the old lore-hungry translateEnchants carry raw
        // § codes / star bars ("Potion - §s§8[⭑⭑⭑⭑⭑], Barrel aged"). Treat those as
        // never-set so this scan refills them with the clean name.
        if (!dn.isEmpty() && (dn.indexOf('§') >= 0 || dn.indexOf('⭑') >= 0)) dn = "";
        if (dn.isEmpty() && sh.item() != null && !sh.item().isEmpty())
            profile.addProperty("display_name", sh.item());
        List<String> effs = translateEnchants(sh.lore());
        if (!effs.isEmpty()) {
            com.google.gson.JsonArray effArr = new com.google.gson.JsonArray();
            effs.forEach(effArr::add);
            profile.add("effects", effArr);
        }

        // Track known hashes (raw item names with suffix)
        com.google.gson.JsonArray hashes;
        if (profile.has("known_hashes") && profile.get("known_hashes").isJsonArray()) {
            hashes = profile.getAsJsonArray("known_hashes");
        } else {
            hashes = new com.google.gson.JsonArray();
            profile.add("known_hashes", hashes);
        }
        String rawHash = sh.itemRaw();
        boolean found = false;
        for (com.google.gson.JsonElement el : hashes)
            if (el.getAsString().equals(rawHash)) { found = true; break; }
        if (!found) hashes.add(rawHash);

        // Keep the reverse index up-to-date
        hashToProfileKey.put(rawHash, key);
        hashToProfileKey.put(sh.item(), key);   // also index the stripped name
    }

    static void loadProfiles() {
        if (configDir == null) return;
        Path file = configDir.resolve("csn_profiles.json");
        if (!Files.exists(file, LinkOption.NOFOLLOW_LINKS)) return;
        try {
            String json = Files.readString(file, StandardCharsets.UTF_8);
            JsonObject root = JsonParser.parseString(json).getAsJsonObject();
            itemProfiles.clear();
            hashToProfileKey.clear();
            for (Map.Entry<String, com.google.gson.JsonElement> entry : root.entrySet()) {
                String key = entry.getKey();
                if (!entry.getValue().isJsonObject()) continue;
                JsonObject profile = entry.getValue().getAsJsonObject();
                itemProfiles.put(key, profile);
                // Rebuild reverse index from known_hashes
                if (profile.has("known_hashes") && profile.get("known_hashes").isJsonArray()) {
                    for (com.google.gson.JsonElement el : profile.getAsJsonArray("known_hashes"))
                        hashToProfileKey.put(el.getAsString(), key);
                }
            }
        } catch (Exception e) {
            System.err.println("[CSN] Failed to load csn_profiles.json: " + e.getMessage());
        }
    }

    static void saveProfiles() {
        if (configDir == null) return;
        try {
            Files.createDirectories(configDir);
            JsonObject root = new JsonObject();
            itemProfiles.forEach(root::add);
            Files.writeString(configDir.resolve("csn_profiles.json"),
                    new Gson().newBuilder().setPrettyPrinting().create().toJson(root),
                    StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
        } catch (IOException e) {
            System.err.println("[CSN] Failed to save csn_profiles.json: " + e.getMessage());
        }
    }

    /** Send a webhook request, honouring Discord's 429 Retry-After (up to 4 attempts).
     *  Returns the final response; the caller checks the status code. */
    private static HttpResponse<String> sendWithRetry(HttpClient http, HttpRequest req)
            throws java.io.IOException, InterruptedException {
        HttpResponse<String> resp = null;
        for (int attempt = 0; attempt < 4; attempt++) {
            resp = http.send(req, HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() != 429) return resp;
            long waitMs = 2_000L * (attempt + 1);
            try {
                String ra = resp.headers().firstValue("Retry-After").orElse("");
                if (!ra.isEmpty()) waitMs = (long) (Double.parseDouble(ra.trim()) * 1000) + 250;
            } catch (Exception ignored) {}
            Thread.sleep(Math.min(Math.max(waitMs, 500), 30_000));
        }
        return resp;
    }

    /** ONE webhook message: a human summary line + attachments. Missing files are skipped;
     *  if no file survives the filter the summary still posts. Replaces the old
     *  one-post-per-file spam (empty stock/profiles posts were pure channel noise). */
    private void postBundle(String webhookUrl, String content, List<Path> files, MinecraftClient client) {
        try {
            String boundary = "CsnBundle" + System.currentTimeMillis();
            java.io.ByteArrayOutputStream body = new java.io.ByteArrayOutputStream();
            body.write(("--" + boundary + "\r\n"
                + "Content-Disposition: form-data; name=\"payload_json\"\r\n"
                + "Content-Type: application/json\r\n\r\n"
                + "{\"content\":\"" + jsonEscape(content) + "\"}\r\n").getBytes(StandardCharsets.UTF_8));
            int i = 0;
            for (Path f : files) {
                if (f == null || !Files.exists(f, LinkOption.NOFOLLOW_LINKS)) continue;
                body.write(("--" + boundary + "\r\n"
                    + "Content-Disposition: form-data; name=\"files[" + i + "]\"; filename=\""
                    + f.getFileName() + "\"\r\n"
                    + "Content-Type: text/plain; charset=utf-8\r\n\r\n").getBytes(StandardCharsets.UTF_8));
                body.write(Files.readAllBytes(f));
                body.write("\r\n".getBytes(StandardCharsets.UTF_8));
                i++;
            }
            body.write(("--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8));

            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(webhookUrl))
                    .timeout(Duration.ofSeconds(30))
                    .header("Content-Type", "multipart/form-data; boundary=" + boundary)
                    .POST(HttpRequest.BodyPublishers.ofByteArray(body.toByteArray()))
                    .build();
            HttpResponse<String> resp = sendWithRetry(
                    HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build(), req);
            if (resp.statusCode() >= 200 && resp.statusCode() < 300) {
                notify(client, "[CSN] \u2705 Report posted to Discord!");
            } else {
                notify(client, "[CSN] Webhook failed (" + resp.statusCode() + ")");
            }
        } catch (Exception e) {
            notify(client, "[CSN] Could not post to Discord: " + e.getMessage());
        }
    }

    private static void notify(MinecraftClient client, String msg) {
        client.execute(() -> {
            if (client.player != null)
                client.player.sendMessage(Text.literal(msg), false);
        });
    }

    /** The two hive items whose sales are paid as harvest wages by the bot. Matches the
     *  bot's hive_value config keys (lower-cased). */
    private static boolean isHiveItem(String item) {
        String n = item == null ? "" : item.trim().toLowerCase(Locale.ROOT);
        return n.equals("honey block") || n.equals("honeycomb block");
    }

    /** Post per-seller honey/comb harvest lines ("&lt;ign&gt; sold you &lt;qty&gt;x &lt;item&gt;")
     *  to the webhook so the Restocker bot's hive autopay pays each harvester by IGN. Only
     *  verb="sold" hive items from THIS run's fresh (.seen-deduped) entries are posted, so
     *  each sale is one timestamped line; the bot dedups on it. Chunked under Discord's
     *  2000-char message limit. Runs on a virtual thread (never blocks the game). */
    private static Path hiveSpoolFile() {
        return configDir == null ? null : configDir.resolve("csn_hive_spool.txt");
    }

    private void postHiveHarvest(String webhookUrl, List<Entry> fresh, MinecraftClient client) {
        try {
            // Spool first: wage lines that failed to post on a PREVIOUS run are retried
            // ahead of this run's lines. runFresh is memory-only and .seen already marks
            // these sales exported, so without the spool a failed post meant the
            // harvesters were silently never paid — the lines existed nowhere.
            List<String> lines = new ArrayList<>();
            Path spool = hiveSpoolFile();
            if (spool != null && Files.exists(spool, LinkOption.NOFOLLOW_LINKS)) {
                try {
                    for (String ln : Files.readAllLines(spool, StandardCharsets.UTF_8))
                        if (!ln.isBlank()) lines.add(ln);
                } catch (IOException e) {
                    System.err.println("[CSN] Could not read hive spool: " + e.getMessage());
                }
            }
            for (Entry e : fresh) {
                if (!"sold".equals(e.verb()) || !isHiveItem(e.item())) continue;
                lines.add(e.actor() + " sold you " + e.quantity() + "x " + e.item()
                        + " @" + e.timestampIso() + " (-0 Coins)");
            }
            if (lines.isEmpty()) return;

            // Chunk into <=1900-char messages (leaves headroom under the 2000 limit),
            // tracking how many LINES each chunk holds so a failure knows exactly which
            // lines remain unposted.
            List<List<String>> chunks = new ArrayList<>();
            List<String> chunk = new ArrayList<>();
            int chunkLen = 0;
            for (String ln : lines) {
                if (chunkLen + ln.length() + 1 > 1900 && !chunk.isEmpty()) {
                    chunks.add(chunk);
                    chunk = new ArrayList<>();
                    chunkLen = 0;
                }
                chunk.add(ln);
                chunkLen += ln.length() + 1;
            }
            if (!chunk.isEmpty()) chunks.add(chunk);

            HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
            int postedLines = 0;
            for (List<String> c : chunks) {
                String json = "{\"content\":\"" + jsonEscape(String.join("\n", c)) + "\"}";
                HttpRequest req = HttpRequest.newBuilder()
                        .uri(URI.create(webhookUrl))
                        .timeout(Duration.ofSeconds(30))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8))
                        .build();
                HttpResponse<String> resp;
                try {
                    resp = sendWithRetry(http, req);
                } catch (Exception ex) {
                    spoolHiveLines(lines.subList(postedLines, lines.size()));
                    notify(client, "[CSN] Hive-pay post failed (" + ex.getMessage() + ") — "
                            + (lines.size() - postedLines) + " wage line(s) saved and will be "
                            + "re-sent on the next export. Nobody loses their pay.");
                    return;
                }
                if (resp.statusCode() < 200 || resp.statusCode() >= 300) {
                    spoolHiveLines(lines.subList(postedLines, lines.size()));
                    notify(client, "[CSN] Hive-pay post failed (" + resp.statusCode() + ") — "
                            + (lines.size() - postedLines) + " wage line(s) saved and will be "
                            + "re-sent on the next export. Nobody loses their pay.");
                    return;
                }
                postedLines += c.size();
                try { Thread.sleep(400); } catch (InterruptedException ignored) {}   // gentle on webhook rate limit
            }
            // Everything posted — the spool (if any) is fully drained.
            if (spool != null) {
                try { Files.deleteIfExists(spool); } catch (IOException ignored) {}
            }
            notify(client, "[CSN] 🐝 Posted " + lines.size() + " harvest line(s) for auto-pay.");
        } catch (Exception e) {
            notify(client, "[CSN] Could not post hive harvest: " + e.getMessage());
        }
    }

    /** Persist unposted wage lines; merged and retried by the next postHiveHarvest. */
    private static void spoolHiveLines(List<String> unposted) {
        Path spool = hiveSpoolFile();
        if (spool == null || unposted.isEmpty()) return;
        try {
            Files.write(spool, unposted, StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
        } catch (IOException e) {
            System.err.println("[CSN] Could not spool hive wage lines: " + e.getMessage());
        }
    }

    /** Minimal JSON string escaper for the webhook "content" body. */
    private static String jsonEscape(String s) {
        StringBuilder b = new StringBuilder();
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"'  -> b.append("\\\"");
                case '\\' -> b.append("\\\\");
                case '\n' -> b.append("\\n");
                case '\r' -> { /* drop */ }
                case '\t' -> b.append("\\t");
                default   -> b.append(c);
            }
        }
        return b.toString();
    }

    /** Null on a READ FAILURE of an existing file (caller must skip the flush and retry)
     *  — returning an empty set there re-exported and re-paid everything as fresh. A
     *  genuinely missing file still returns an empty set (nothing seen yet). */
    private static Set<String> loadSeen(Path seenFile) {
        if (!Files.exists(seenFile, LinkOption.NOFOLLOW_LINKS)) return new HashSet<>();
        try {
            return new HashSet<>(Files.readAllLines(seenFile, StandardCharsets.UTF_8));
        } catch (IOException e) {
            System.err.println("[CSN] Could not read .seen (" + e.getMessage() + ") — flush deferred");
            return null;
        }
    }

    private static void saveSeen(Path seenFile, Set<String> hashes) {
        List<String> sorted = new ArrayList<>(hashes);
        Collections.sort(sorted);
        try {
            Files.write(seenFile, sorted, StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
        } catch (IOException e) {
            e.printStackTrace();
        }
    }

    // ── Per-sale stable identity ─────────────────────────────────────────────
    // saleUid is the ONE identity a sale carries across the whole pipeline: it de-dups
    // the mod's .seen set AND ships in the CSV's sale_uid column so the bot keys its
    // csn_transactions dedup on the exact same value — the two sides can no longer
    // disagree about which sales are duplicates.
    //
    // MINUTE bucket (was: hour). The hour bucket silently dropped a second identical
    // sale 40 minutes after the first. The reconstructed timestamp ("Xd Yh Zm ago",
    // minute precision) can drift ±1 minute between re-reads, so isSeen() also checks
    // the two ADJACENT minute buckets — re-reads still dedup, genuinely repeated sales
    // ≥2 minutes apart are counted.
    //
    // NOTE: new format, so old .seen files no longer match directly; isSeen() also
    // checks the LEGACY hour-bucket hash so an in-flight period file cuts over cleanly.

    private static String minuteBucketOf(String tsIso) {
        return (tsIso != null && tsIso.length() >= 16) ? tsIso.substring(0, 16) : "";
    }

    private static String uidForMinute(Entry e, String minuteBucket) {
        // itemRaw (with the #code) keeps two DIFFERENT potion variants sold in the same
        // minute at the same price from colliding into one uid.
        String raw = e.actor() + "|" + e.seller() + "|" + e.verb() + "|" + e.quantity()
                   + "|" + e.itemRaw() + "|" + e.amountCoins() + "|" + minuteBucket;
        return sha256(raw).substring(0, 32);
    }

    static String saleUid(Entry e) {
        return uidForMinute(e, minuteBucketOf(e.timestampIso()));
    }

    /** True if this sale was already exported: canonical minute, either adjacent minute
     *  (reconstruction drift), or the legacy hour-bucket hash from a pre-upgrade .seen. */
    private static boolean isSeen(Set<String> seen, Entry e) {
        String ts = e.timestampIso();
        if (seen.contains(uidForMinute(e, minuteBucketOf(ts)))) return true;
        try {
            Instant t = Instant.parse(ts);
            if (seen.contains(uidForMinute(e, minuteBucketOf(t.minusSeconds(60).toString())))) return true;
            if (seen.contains(uidForMinute(e, minuteBucketOf(t.plusSeconds(60).toString()))))  return true;
        } catch (Exception ignored) {}
        return seen.contains(legacyEntryHash(e));
    }

    /** The pre-upgrade hash format (hour bucket, no seller, full sha256) — checked only,
     *  never written, so existing .seen files keep de-duping through the cutover. */
    private static String legacyEntryHash(Entry e) {
        String ts = e.timestampIso();
        String hourBucket = (ts != null && ts.length() >= 13) ? ts.substring(0, 13) : "";
        // The old build stored the item with only HEX codes stripped — reproduce that
        // exactly so an in-flight period's .seen keeps deduping through the cutover.
        String legacyItem = e.itemRaw().replaceAll("#[0-9a-fA-F]{1,6}$", "").trim();
        String raw = e.actor() + "|" + e.verb() + "|" + e.quantity()
                   + "|" + legacyItem + "|" + e.amountCoins() + "|" + hourBucket;
        return sha256(raw);
    }

    private static String sha256(String value) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest    = md.digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(64);
            for (byte b : digest) sb.append(String.format("%02x", b));
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            return value;
        }
    }

    private static String fmt(double v) {
        return new BigDecimal(v)
                .setScale(2, RoundingMode.HALF_UP)
                .stripTrailingZeros()
                .toPlainString();
    }

    private static String csvField(String s) {
        if (s.contains(",") || s.contains("\"") || s.contains("\n") || s.contains("\r")) {
            return "\"" + s.replace("\"", "\"\"") + "\"";
        }
        return s;
    }

    private static int parseOrZero(String s) {
        if (s == null || s.isEmpty()) return 0;
        try { return Integer.parseInt(s.trim()); }
        catch (NumberFormatException e) { return 0; }
    }

    private static double parseCoins(String s) {
        try { return Double.parseDouble(s.replace(",", "")); }
        catch (NumberFormatException e) { return 0; }
    }

    private static void sendCommand(MinecraftClient client, String cmdNoSlash) {
        if (client.player != null)
            client.player.networkHandler.sendChatCommand(cmdNoSlash);
    }
}
