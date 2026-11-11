package de.weihnachtsdorf.bmbridge;

import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.event.Cancellable;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.AsyncPlayerChatEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.server.RemoteServerCommandEvent;
import org.bukkit.event.server.ServerCommandEvent;
import org.bukkit.metadata.MetadataValue;
import org.bukkit.plugin.java.JavaPlugin;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Duration;
import java.util.Collection;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class BlueMapLiveBridge extends JavaPlugin implements Listener {

    private Path outFile;
    private Path tmpFile;
    private String lastPayload = "";

    private static final HttpClient HTTP_CLIENT = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(2))
            .build();
    private URI chatEndpoint = URI.create("http://192.168.1.150:8000/api/bluemap/chat/ingame");

    @Override
    public void onEnable() {
        saveDefaultConfig();
        String endpointStr = getConfig().getString("endpoint", "http://192.168.1.150:8000/api/bluemap/chat/ingame");
        try {
            chatEndpoint = URI.create(endpointStr);
        } catch (Exception e) {
            getLogger().warning("Invalid endpoint in config.yml, using default: " + e.getMessage());
            chatEndpoint = URI.create("http://192.168.1.150:8000/api/bluemap/chat/ingame");
        }

        try {
            Path webDir = Path.of("web");
            Files.createDirectories(webDir);
            outFile = webDir.resolve("players.json");
            tmpFile = webDir.resolve("players.json.tmp");
        } catch (Exception e) {
            getLogger().warning("Failed to initialize web export paths: " + e.getMessage());
            return;
        }

        // Export player positions every 2 seconds (40 ticks)
        Bukkit.getScheduler().runTaskTimer(this, this::exportPlayers, 40L, 40L);

        // Register chat and lifecycle events to bridge to BlueMap Web Chat
        getServer().getPluginManager().registerEvents(this, this);

        getLogger().info("BlueMapLiveBridge enabled (2s live player exporter & chat bridge).");
    }

    @Override
    public void onDisable() {
        writeAsync("{\"players\":[]}");
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onChat(AsyncPlayerChatEvent event) {
        if (isVanished(event.getPlayer())) {
            return;
        }
        postEvent("chat", event.getPlayer().getUniqueId().toString(), event.getPlayer().getName(), event.getMessage());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onJoin(PlayerJoinEvent event) {
        if (event.getJoinMessage() == null || isVanished(event.getPlayer())) {
            return;
        }
        postEvent("join", event.getPlayer().getUniqueId().toString(), event.getPlayer().getName(), "");
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        if (event.getQuitMessage() == null || isVanished(event.getPlayer())) {
            return;
        }
        postEvent("quit", event.getPlayer().getUniqueId().toString(), event.getPlayer().getName(), "");
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onDeath(PlayerDeathEvent event) {
        if (isVanished(event.getEntity()) || event.getDeathMessage() == null || event.getDeathMessage().isBlank()) {
            return;
        }
        postEvent("death", event.getEntity().getUniqueId().toString(), event.getEntity().getName(), event.getDeathMessage());
    }

    private boolean isVanished(Player player) {
        if (player == null) {
            return false;
        }
        for (String metaKey : new String[]{"vanished", "vanish"}) {
            if (player.hasMetadata(metaKey)) {
                for (MetadataValue mv : player.getMetadata(metaKey)) {
                    if (mv.asBoolean()) {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    private void postEvent(String type, String uuid, String username, String message) {
        try {
            String json = String.format(
                    "{\"type\":\"%s\",\"uuid\":\"%s\",\"username\":\"%s\",\"message\":\"%s\"}",
                    escape(type), escape(uuid), escape(username), escape(message)
            );
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(chatEndpoint)
                    .header("Content-Type", "application/json")
                    .timeout(Duration.ofSeconds(3))
                    .POST(HttpRequest.BodyPublishers.ofString(json, StandardCharsets.UTF_8))
                    .build();
            HTTP_CLIENT.sendAsync(request, HttpResponse.BodyHandlers.discarding())
                    .exceptionally(ex -> null);
        } catch (Exception ignored) {
        }
    }

    private static String escape(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '\\': sb.append("\\\\"); break;
                case '"': sb.append("\\\""); break;
                case '\n':
                case '\r':
                case '\t': sb.append(' '); break;
                default:
                    if (c >= 32) {
                        sb.append(c);
                    }
                    break;
            }
        }
        return sb.toString();
    }

    private void exportPlayers() {
        StringBuilder sb = new StringBuilder(256);
        sb.append("{\"players\":[");
        boolean first = true;
        Collection<? extends Player> onlinePlayers = Bukkit.getOnlinePlayers();
        for (Player p : onlinePlayers) {
            if (p.getGameMode() == GameMode.SPECTATOR || p.isInvisible() || isVanished(p)) {
                continue;
            }
            Location loc = p.getLocation();
            World world = loc.getWorld();
            boolean foreign = (world == null || world.getEnvironment() != World.Environment.NORMAL);

            if (!first) {
                sb.append(',');
            }
            first = false;

            String safeName = p.getName().replace("\\", "\\\\").replace("\"", "\\\"");
            sb.append(String.format(
                    Locale.ROOT,
                    "{\"uuid\":\"%s\",\"name\":\"%s\",\"foreign\":%s,\"position\":{\"x\":%.2f,\"y\":%.2f,\"z\":%.2f},\"rotation\":{\"pitch\":%.1f,\"yaw\":%.1f,\"roll\":0}}",
                    p.getUniqueId().toString(),
                    safeName,
                    foreign ? "true" : "false",
                    loc.getX(),
                    loc.getY(),
                    loc.getZ(),
                    loc.getPitch(),
                    loc.getYaw()
            ));
        }
        sb.append("]}");
        String payload = sb.toString();
        if (!payload.equals(lastPayload)) {
            lastPayload = payload;
            writeAsync(payload);
        }
    }

    private static final Pattern TEXT_PATTERN = Pattern.compile("\"text\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"");

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onServerCommand(ServerCommandEvent event) {
        processTellrawEvent(event.getCommand(), event);
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onRemoteServerCommand(RemoteServerCommandEvent event) {
        processTellrawEvent(event.getCommand(), event);
    }

    private void processTellrawEvent(String rawCmd, Cancellable event) {
        if (rawCmd == null) {
            return;
        }
        String cmd = rawCmd.trim();
        if (cmd.startsWith("/")) {
            cmd = cmd.substring(1).trim();
        }
        int idx = cmd.indexOf("tellraw @a ");
        if (idx == -1) {
            return;
        }
        String jsonPart = cmd.substring(idx + "tellraw @a ".length()).trim();
        if (jsonPart.contains("[D] ") || jsonPart.contains("[Web] ") || jsonPart.contains("[Weihnachtsdorf]")) {
            String plainText = extractPlainText(jsonPart);
            if (!plainText.isEmpty()) {
                Bukkit.getConsoleSender().sendMessage(plainText);
            }
            if (Bukkit.getOnlinePlayers().isEmpty()) {
                event.setCancelled(true);
            }
        }
    }

    private String extractPlainText(String json) {
        StringBuilder sb = new StringBuilder();
        Matcher m = TEXT_PATTERN.matcher(json);
        while (m.find()) {
            sb.append(unescapeJson(m.group(1)));
        }
        return sb.toString();
    }

    private static String unescapeJson(String s) {
        if (s == null || s.isEmpty()) return "";
        StringBuilder sb = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '\\' && i + 1 < s.length()) {
                char next = s.charAt(i + 1);
                switch (next) {
                    case '"': sb.append('"'); i++; break;
                    case '\\': sb.append('\\'); i++; break;
                    case '/': sb.append('/'); i++; break;
                    case 'b': sb.append('\b'); i++; break;
                    case 'f': sb.append('\f'); i++; break;
                    case 'n': sb.append('\n'); i++; break;
                    case 'r': sb.append('\r'); i++; break;
                    case 't': sb.append('\t'); i++; break;
                    case 'u':
                        if (i + 5 < s.length()) {
                            try {
                                int code = Integer.parseInt(s.substring(i + 2, i + 6), 16);
                                sb.append((char) code);
                                i += 5;
                            } catch (NumberFormatException ignored) {
                                sb.append(c);
                            }
                        } else {
                            sb.append(c);
                        }
                        break;
                    default:
                        sb.append(next);
                        i++;
                        break;
                }
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    private void writeAsync(String payload) {
        Bukkit.getScheduler().runTaskAsynchronously(this, () -> {
            try {
                Files.writeString(tmpFile, payload, StandardCharsets.UTF_8);
                Files.move(tmpFile, outFile, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            } catch (Exception ignored) {
            }
        });
    }
}
