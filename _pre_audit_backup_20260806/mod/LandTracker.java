package com.vaicos.csnexport;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.keybinding.v1.KeyBindingHelper;
import net.fabricmc.fabric.api.client.message.v1.ClientSendMessageEvents;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.gui.screen.ingame.HandledScreen;
import net.minecraft.client.option.KeyBinding;
import net.minecraft.client.util.InputUtil;
import org.lwjgl.glfw.GLFW;
import net.minecraft.item.Item;
import net.minecraft.item.ItemStack;
import net.minecraft.item.tooltip.TooltipType;
import net.minecraft.screen.slot.Slot;
import net.minecraft.text.Text;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.Duration;
import java.time.Instant;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Lands (claims) tracker — treasuries + teleport-fee income for the Restocker bot.
 *
 * What it does:
 *  1. BALANCE SWEEP (keybind): sends "/la balance <land>" for every land in
 *     sales/lands_config.json and parses the replies. Also parses balances whenever
 *     the PLAYER runs the command by hand (we watch outgoing commands).
 *  2. INBOX SCAN (automatic): whenever a Lands inbox screen is open, every paper's
 *     tooltip ("#29 07/15/2026 11:00 / Received total of $250.00 taxes ... New
 *     balance: $6,314,340.00 / Click to remove.") is captured. Entries are attributed
 *     to the last land menu that was open ("Land MardURAK" -> MardURAK).
 *  3. FORWARDING: new entries + balances are posted to the Discord webhook in a
 *     machine-parseable format the bot ingests (LANDS-BAL / LANDS-ENTRY lines).
 *     Everything is forwarded VERBATIM — the bot classifies deposits, withdrawals,
 *     taxes and teleport fees server-side, so new entry formats never lose data.
 *
 * Dedup is persistent (sales/lands_seen.txt), so re-opening the inbox or leaving
 * old messages unremoved never double-posts.
 */
public final class LandTracker {

    private static final long   SWEEP_CMD_SPACING_MS = 2_000;  // between /la balance commands
    private static final long   BALANCE_WINDOW_MS    = 6_000;  // how long a sent command may wait for its reply
    private static final long   POST_DEBOUNCE_MS     = 8_000;  // batch findings before posting
    private static final int    MAX_POST_CHARS       = 1_800;  // stay under Discord's 2000/message

    // "#29" + "07/15/2026 11:00" — first tooltip line of an inbox paper
    private static final Pattern INBOX_HEAD_RE = Pattern.compile(
            "^\\s*#(\\d+)\\s+(\\d{2}/\\d{2}/\\d{4}\\s+\\d{2}:\\d{2})\\s*$");
    // any money amount, e.g. $6,314,340.00
    private static final Pattern MONEY_RE = Pattern.compile("\\$([\\d,]+(?:\\.\\d+)?)");
    // a balance-looking chat line (used only inside the post-command window)
    private static final Pattern BALANCE_LINE_RE = Pattern.compile(
            "(?i)balance[^$]*\\$([\\d,]+(?:\\.\\d+)?)");
    // outgoing command forms we care about: la/land/lands balance <name>
    private static final Pattern BALANCE_CMD_RE = Pattern.compile(
            "(?i)^(?:la|land|lands)\\s+balance\\s+(\\S+)\\s*$");
    // land menu screen title: "Land MardURAK"
    private static final Pattern LAND_TITLE_RE = Pattern.compile("^Land\\s+(.+)$");

    private static KeyBinding sweepKey;

    private static List<String> lands = new ArrayList<>();   // configured land names
    private static String landsWebhook = "";                 // falls back to the CSN webhook

    // sweep state
    private static boolean sweeping = false;
    private static int     sweepIdx = 0;
    private static long    nextSweepCmdAtMs = 0;

    // "we just asked for this land's balance" window
    private static String  expectLand = null;
    private static long    expectUntilMs = 0;

    // inbox attribution: the last land whose menu was open
    private static String  currentLand = "";

    // findings queue
    private static final Map<String, Double> pendingBalances = new LinkedHashMap<>();
    private static final List<String>        pendingEntries  = new ArrayList<>();
    private static long lastFindingAtMs = 0;

    private static Set<String> seen = null;      // persistent entry keys
    private static boolean configLoaded = false;

    private static boolean autoSweepOnExport = true;

    private LandTracker() {}

    /** Called by CsnExportClient when a CSN export finishes — every sales run also
     *  snapshots every configured land balance. Teleport fees are inferred from the
     *  GAPS between balance checkpoints, so the more checkpoints, the sharper the
     *  fee picture (the owner's "every run of the CSN mod" rule). */
    static void autoSweep(MinecraftClient client) {
        if (!autoSweepOnExport || sweeping || lands.isEmpty()) return;
        sweeping = true;
        sweepIdx = 0;
        nextSweepCmdAtMs = System.currentTimeMillis() + 2_000;  // let the export's chat settle
        say(client, "[Lands] Post-export balance sweep of " + lands.size() + " land(s)…");
    }

    // ── wiring (called from CsnExportClient.onInitializeClient) ──────────────
    static void init(KeyBinding.Category category) {
        sweepKey = KeyBindingHelper.registerKeyBinding(new KeyBinding(
                "key.csnexport.lands",
                InputUtil.Type.KEYSYM,
                GLFW.GLFW_KEY_F8,
                category
        ));
        ClientTickEvents.END_CLIENT_TICK.register(LandTracker::onEndTick);
        // watch the player's own commands so hand-typed /la balance replies attribute too
        ClientSendMessageEvents.COMMAND.register(command -> {
            Matcher m = BALANCE_CMD_RE.matcher(command.strip());
            if (m.matches()) {
                expectLand    = m.group(1);
                expectUntilMs = System.currentTimeMillis() + BALANCE_WINDOW_MS;
            }
        });
    }

    // ── config: sales/lands_config.json ─────────────────────────────────────
    // AUDIT FIX: this used to auto-create with "lands": ["MardURAK"] hardcoded as the
    // default — the owner's OWN claim name, baked into every fresh install. A new market
    // owner who never opened this file (or the settings screen below) would silently sweep
    // and report on the owner's land through THEIR OWN webhook, corrupting whichever market
    // that land is bound to on the bot side. A fresh install now defaults to an EMPTY lands
    // list — nothing is tracked until the player deliberately adds their own claim name via
    // the "Your Land Claim Name(s)" field in Mod Menu > CSN Export Settings.
    static void ensureConfig(MinecraftClient client) {
        // Same reason as saveLandsConfig: without this the settings screen opened from
        // the title screen shows an EMPTY land field even when one is configured.
        if (client != null) CsnExportClient.ensureConfigDir(client);
        if (configLoaded || CsnExportClient.configDir == null) return;
        configLoaded = true;
        Path f = CsnExportClient.configDir.resolve("lands_config.json");
        if (!Files.exists(f, LinkOption.NOFOLLOW_LINKS)) {
            try {
                Files.createDirectories(f.getParent());
                Files.writeString(f,
                    "{\n  \"lands\": [],\n  \"lands_webhook\": \"\",\n  \"auto_sweep_on_export\": true\n}\n",
                    StandardCharsets.UTF_8, StandardOpenOption.CREATE_NEW);
            } catch (Exception ignored) {}
        }
        try {
            JsonObject root = JsonParser.parseString(
                    Files.readString(f, StandardCharsets.UTF_8)).getAsJsonObject();
            lands = new ArrayList<>();
            if (root.has("lands") && root.get("lands").isJsonArray()) {
                for (var el : root.getAsJsonArray("lands")) {
                    String s = el.getAsString().strip();
                    if (!s.isEmpty()) lands.add(s);
                }
            }
            if (root.has("lands_webhook") && !root.get("lands_webhook").isJsonNull())
                landsWebhook = root.get("lands_webhook").getAsString().strip();
            if (root.has("auto_sweep_on_export"))
                autoSweepOnExport = root.get("auto_sweep_on_export").getAsBoolean();
        } catch (Exception e) {
            System.err.println("[Lands] Could not read lands_config.json: " + e.getMessage());
        }
        if (seen == null) seen = loadSeen();
    }

    private static String webhook() {
        return landsWebhook.isEmpty() ? CsnExportClient.discordWebhook : landsWebhook;
    }

    // ── settings-screen access ────────────────────────────────────────────
    // Package-private so CsnSettingsScreen (same package) can show/edit the configured
    // claim name(s) instead of players hand-editing lands_config.json.
    static List<String> getLands() {
        return new ArrayList<>(lands);
    }

    static void setLands(List<String> newLands) {
        List<String> cleaned = new ArrayList<>();
        for (String s : newLands) {
            String t = s == null ? "" : s.strip();
            if (!t.isEmpty()) cleaned.add(t);
        }
        lands = cleaned;
    }

    static void saveLandsConfig() {
        // BUG: this used to `return` when configDir was null, silently discarding the
        // save. configDir is only set on world join, but Mod Menu opens the settings
        // screen from the TITLE screen — so editing your land there looked like it
        // worked and wrote nothing. Initialise it here instead of dropping the write.
        CsnExportClient.ensureConfigDir();
        if (CsnExportClient.configDir == null) return;
        try {
            Files.createDirectories(CsnExportClient.configDir);
            JsonObject root = new JsonObject();
            JsonArray arr = new JsonArray();
            for (String l : lands) arr.add(l);
            root.add("lands", arr);
            root.addProperty("lands_webhook", landsWebhook);
            root.addProperty("auto_sweep_on_export", autoSweepOnExport);
            Files.writeString(CsnExportClient.configDir.resolve("lands_config.json"),
                    new GsonBuilder().setPrettyPrinting().create().toJson(root),
                    StandardCharsets.UTF_8);
        } catch (Exception e) {
            System.err.println("[Lands] Could not save lands_config.json: " + e.getMessage());
        }
    }

    // ── tick: keybind, sweep pacing, inbox screens, debounce-post ────────────
    private static void onEndTick(MinecraftClient client) {
        if (client.player == null) return;
        ensureConfig(client);
        long now = System.currentTimeMillis();

        if (sweepKey.wasPressed()) {
            if (sweeping) {
                say(client, "[Lands] Sweep already running…");
            } else if (lands.isEmpty()) {
                say(client, "[Lands] No lands configured — add names to sales/lands_config.json");
            } else {
                sweeping = true;
                sweepIdx = 0;
                nextSweepCmdAtMs = now;
                say(client, "[Lands] Balance sweep of " + lands.size() + " land(s) started…");
            }
        }

        if (sweeping && now >= nextSweepCmdAtMs) {
            if (sweepIdx >= lands.size()) {
                sweeping = false;
                say(client, "[Lands] Sweep done — balances will post shortly.");
            } else {
                String land = lands.get(sweepIdx++);
                expectLand    = land;
                expectUntilMs = now + BALANCE_WINDOW_MS;
                client.player.networkHandler.sendChatCommand("la balance " + land);
                nextSweepCmdAtMs = now + SWEEP_CMD_SPACING_MS;
            }
        }

        scanOpenScreen(client);

        // debounce-post the findings so one inbox open = one webhook message
        if (lastFindingAtMs > 0 && (now - lastFindingAtMs) >= POST_DEBOUNCE_MS
                && (!pendingBalances.isEmpty() || !pendingEntries.isEmpty())) {
            flushFindings(client);
        }
    }

    // ── chat: balance replies (only inside the post-command window) ──────────
    /** Called from CsnExportClient.onReceiveGameMessage for EVERY chat line. */
    static void onChat(String line) {
        if (expectLand == null || System.currentTimeMillis() > expectUntilMs) return;
        for (String l : line.split("\\r?\\n")) {
            Matcher m = BALANCE_LINE_RE.matcher(l);
            if (m.find()) {
                double bal = parseMoney(m.group(1));
                pendingBalances.put(expectLand, bal);
                lastFindingAtMs = System.currentTimeMillis();
                expectLand = null;
                return;
            }
        }
    }

    // ── screens: remember the land menu, harvest inbox papers ────────────────
    private static void scanOpenScreen(MinecraftClient client) {
        if (!(client.currentScreen instanceof HandledScreen<?> hs)) return;
        String title = hs.getTitle().getString().strip();

        Matcher lm = LAND_TITLE_RE.matcher(title);
        if (lm.matches()) {
            currentLand = lm.group(1).strip();
            return;
        }
        if (!title.toLowerCase(Locale.ROOT).contains("inbox")) return;

        // An inbox screen: every paper's tooltip is one ledger entry.
        for (Slot slot : hs.getScreenHandler().slots) {
            ItemStack stack = slot.getStack();
            if (stack.isEmpty()) continue;
            List<Text> tip;
            try {
                tip = stack.getTooltip(Item.TooltipContext.DEFAULT, client.player, TooltipType.BASIC);
            } catch (Exception e) {
                continue;
            }
            if (tip == null || tip.size() < 2) continue;

            String head = tip.get(0).getString().strip();
            Matcher hm = INBOX_HEAD_RE.matcher(head);
            if (!hm.matches()) continue;

            StringBuilder body = new StringBuilder();
            for (int i = 1; i < tip.size(); i++) {
                String t = tip.get(i).getString().strip();
                if (t.isEmpty() || t.toLowerCase(Locale.ROOT).contains("click to remove")) continue;
                if (body.length() > 0) body.append(' ');
                body.append(t);
            }
            if (body.length() == 0) continue;

            String land = currentLand.isEmpty() ? "unknown" : currentLand;
            String key  = land + "|" + hm.group(1) + "|" + hm.group(2) + "|" + body;
            if (!seen.add(key)) continue;

            pendingEntries.add("LANDS-ENTRY|" + land + "|#" + hm.group(1) + "|" + hm.group(2) + "|" + body);
            lastFindingAtMs = System.currentTimeMillis();
        }
    }

    // ── posting ──────────────────────────────────────────────────────────────
    private static void flushFindings(MinecraftClient client) {
        String hook = webhook();
        List<String> lines = new ArrayList<>();
        pendingBalances.forEach((land, bal) ->
                lines.add("LANDS-BAL|" + land + "|" + String.format(Locale.ROOT, "%.2f", bal)
                          + "|" + Instant.now().toString()));
        lines.addAll(pendingEntries);
        int nBal = pendingBalances.size(), nEnt = pendingEntries.size();
        pendingBalances.clear();
        pendingEntries.clear();
        lastFindingAtMs = 0;
        saveSeen();

        if (hook.isEmpty()) {
            say(client, "[Lands] " + nBal + " balance(s), " + nEnt
                        + " new entrie(s) captured — set a webhook to auto-post.");
            return;
        }

        // chunk to stay under Discord's message limit
        List<String> chunks = new ArrayList<>();
        StringBuilder cur = new StringBuilder("🏦 LANDS FEED");
        for (String l : lines) {
            if (cur.length() + l.length() + 1 > MAX_POST_CHARS) {
                chunks.add(cur.toString());
                cur = new StringBuilder("🏦 LANDS FEED (cont.)");
            }
            cur.append('\n').append(l);
        }
        chunks.add(cur.toString());

        Thread t = new Thread(() -> {
            try {
                HttpClient http = HttpClient.newBuilder()
                        .connectTimeout(Duration.ofSeconds(10)).build();
                for (String chunk : chunks) {
                    JsonObject payload = new JsonObject();
                    payload.addProperty("content", chunk);
                    HttpRequest req = HttpRequest.newBuilder()
                            .uri(URI.create(hook))
                            .timeout(Duration.ofSeconds(30))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString(
                                    new Gson().toJson(payload), StandardCharsets.UTF_8))
                            .build();
                    HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
                    if (resp.statusCode() < 200 || resp.statusCode() >= 300) {
                        say(client, "[Lands] Webhook failed (" + resp.statusCode() + ")");
                        return;
                    }
                    Thread.sleep(400);   // stay clear of webhook rate limits
                }
                say(client, "[Lands] ✅ Posted " + nBal + " balance(s), " + nEnt + " ledger entrie(s).");
            } catch (Exception e) {
                say(client, "[Lands] Could not post: " + e.getMessage());
            }
        }, "lands-webhook");
        t.setDaemon(true);
        t.start();
    }

    // ── seen persistence ─────────────────────────────────────────────────────
    private static Path seenFile() {
        return CsnExportClient.configDir.resolve("lands_seen.txt");
    }

    private static Set<String> loadSeen() {
        try {
            if (Files.exists(seenFile(), LinkOption.NOFOLLOW_LINKS))
                return new HashSet<>(Files.readAllLines(seenFile(), StandardCharsets.UTF_8));
        } catch (Exception e) {
            // Say it: an unreadable lands_seen.txt means every tracked event re-posts as
            // new on the next sweep (duplicate treasury/inbox lines in Discord).
            System.err.println("[CSN lands] Could not read lands_seen.txt: " + e.getMessage());
        }
        return new HashSet<>();
    }

    private static void saveSeen() {
        try {
            List<String> sorted = new ArrayList<>(seen);
            Collections.sort(sorted);
            Files.createDirectories(seenFile().getParent());
            Files.write(seenFile(), sorted, StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
        } catch (Exception e) {
            System.err.println("[Lands] Could not save lands_seen.txt: " + e.getMessage());
        }
    }

    private static double parseMoney(String s) {
        try { return Double.parseDouble(s.replace(",", "")); }
        catch (NumberFormatException e) { return 0; }
    }

    private static void say(MinecraftClient client, String msg) {
        client.execute(() -> {
            if (client.player != null)
                client.player.sendMessage(Text.literal(msg), false);
        });
    }
}
