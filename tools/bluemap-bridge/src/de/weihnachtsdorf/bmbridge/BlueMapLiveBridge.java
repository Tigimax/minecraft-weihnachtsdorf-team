package de.weihnachtsdorf.bmbridge;

import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.World;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.AsyncPlayerChatEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
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

public class BlueMapLiveBridge extends JavaPlugin implements Listener {

    private Path outFile;
    private Path tmpFile;
    private String lastPayload = "";

    private static final HttpClient HTTP_CLIENT = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(2))
            .build();
    private static final URI CHAT_ENDPOINT = URI.create("http://192.168.1.150:8000/api/bluemap/chat/ingame");

    @Override
    public void onEnable() {
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
        if (isVanished(event.getPlayer())) {
            return;
        }
        postEvent("join", event.getPlayer().getUniqueId().toString(), event.getPlayer().getName(), "");
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onQuit(PlayerQuitEvent event) {
        if (isVanished(event.getPlayer())) {
            return;
        }
        postEvent("quit", event.getPlayer().getUniqueId().toString(), event.getPlayer().getName(), "");
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onDeath(PlayerDeathEvent event) {
        if (isVanished(event.getEntity())) {
            return;
        }
        String deathMsg = event.getDeathMessage() != null ? event.getDeathMessage() : "";
        postEvent("death", event.getEntity().getUniqueId().toString(), event.getEntity().getName(), deathMsg);
    }

    private boolean isVanished(Player player) {
        if (player == null) {
            return false;
        }
        if (player.hasMetadata("vanished")) {
            for (MetadataValue mv : player.getMetadata("vanished")) {
                if (mv.asBoolean()) {
                    return true;
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
                    .uri(CHAT_ENDPOINT)
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
        return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ").replace("\r", "");
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
