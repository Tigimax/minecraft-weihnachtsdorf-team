package com.tigimax.voicemouthrelay;

import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.player.PlayerRegisterChannelEvent;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.plugin.messaging.PluginMessageListener;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

public final class VoiceMouthRelayPlugin extends JavaPlugin implements PluginMessageListener, Listener {

    private static final String SELECT_MOUTH_C2S = "voicemouth:select_mouth_c2s";
    private static final String SELECT_POS_C2S   = "voicemouth:select_position_c2s";
    private static final String SYNC_MOUTH_S2C   = "voicemouth:sync_mouth_s2c";
    private static final String SYNC_POS_S2C     = "voicemouth:sync_position_s2c";

    private static final String MOUTH_OFF      = "voicemouth:textures/entity/mouth_off.png";
    private static final String MOUTH_STANDARD = "voicemouth:textures/entity/mouth_standard.png";

    private static final byte[] RAW_MOUTH_OFF = encodeVarString(MOUTH_OFF);
    private static final byte[] RAW_MOUTH_STD = encodeVarString(MOUTH_STANDARD);

    private final Set<UUID> enabled = ConcurrentHashMap.newKeySet();
    private final Map<UUID, byte[]> chosenMouths = new ConcurrentHashMap<>();
    private final Map<UUID, byte[]> positions = new ConcurrentHashMap<>();

    @Override
    public void onEnable() {
        var messenger = getServer().getMessenger();
        messenger.registerIncomingPluginChannel(this, SELECT_MOUTH_C2S, this);
        messenger.registerIncomingPluginChannel(this, SELECT_POS_C2S, this);
        messenger.registerOutgoingPluginChannel(this, SYNC_MOUTH_S2C);
        messenger.registerOutgoingPluginChannel(this, SYNC_POS_S2C);
        getServer().getPluginManager().registerEvents(this, this);
        getLogger().info("VoiceMouthRelay enabled (/mouth toggle, default OFF).");
    }

    @Override
    public void onDisable() {
        getServer().getMessenger().unregisterIncomingPluginChannel(this);
        getServer().getMessenger().unregisterOutgoingPluginChannel(this);
        enabled.clear();
        chosenMouths.clear();
        positions.clear();
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player)) {
            sender.sendMessage("Nur fuer Spieler verfuegbar.");
            return true;
        }
        UUID uuid = player.getUniqueId();
        boolean nowOn;
        if (args.length > 0 && (args[0].equalsIgnoreCase("on") || args[0].equalsIgnoreCase("an"))) {
            enabled.add(uuid);
            nowOn = true;
        } else if (args.length > 0 && (args[0].equalsIgnoreCase("off") || args[0].equalsIgnoreCase("aus"))) {
            enabled.remove(uuid);
            nowOn = false;
        } else {
            nowOn = enabled.contains(uuid) ? !enabled.remove(uuid) : enabled.add(uuid);
        }

        byte[] raw = nowOn ? chosenMouths.getOrDefault(uuid, RAW_MOUTH_STD) : RAW_MOUTH_OFF;
        broadcast(SYNC_MOUTH_S2C, prependUuid(uuid, raw));

        if (nowOn) {
            player.sendMessage("§a[VoiceMouth] §fSprechender Mund ist jetzt §a§lAN§f. §7(/mouth zum Ausschalten)");
        } else {
            player.sendMessage("§7[VoiceMouth] §fSprechender Mund ist jetzt §c§lAUS§f. §7(/mouth zum Einschalten)");
        }
        return true;
    }

    @Override
    public void onPluginMessageReceived(String channel, Player sender, byte[] message) {
        UUID uuid = sender.getUniqueId();
        if (SELECT_MOUTH_C2S.equals(channel)) {
            String mouthId = decodeVarString(message);
            if (mouthId == null) return;
            if (MOUTH_OFF.equals(mouthId)) {
                enabled.remove(uuid);
                broadcast(SYNC_MOUTH_S2C, prependUuid(uuid, RAW_MOUTH_OFF));
            } else {
                chosenMouths.put(uuid, message.clone());
                enabled.add(uuid);
                broadcast(SYNC_MOUTH_S2C, prependUuid(uuid, message));
            }
        } else if (SELECT_POS_C2S.equals(channel)) {
            if (!isValidPositionPayload(message)) return;
            positions.put(uuid, message.clone());
            broadcast(SYNC_POS_S2C, prependUuid(uuid, message));
        }
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent event) {
        UUID uuid = event.getPlayer().getUniqueId();
        // Ensure new player starts hidden for everyone until they enable /mouth
        byte[] raw = enabled.contains(uuid) ? chosenMouths.getOrDefault(uuid, RAW_MOUTH_STD) : RAW_MOUTH_OFF;
        broadcast(SYNC_MOUTH_S2C, prependUuid(uuid, raw));
    }

    @EventHandler
    public void onChannelRegister(PlayerRegisterChannelEvent event) {
        Player p = event.getPlayer();
        if (SYNC_MOUTH_S2C.equals(event.getChannel())) {
            for (Player other : Bukkit.getOnlinePlayers()) {
                UUID u = other.getUniqueId();
                byte[] raw = enabled.contains(u) ? chosenMouths.getOrDefault(u, RAW_MOUTH_STD) : RAW_MOUTH_OFF;
                p.sendPluginMessage(this, SYNC_MOUTH_S2C, prependUuid(u, raw));
            }
        } else if (SYNC_POS_S2C.equals(event.getChannel())) {
            positions.forEach((uuid, raw) -> p.sendPluginMessage(this, SYNC_POS_S2C, prependUuid(uuid, raw)));
        }
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        UUID uuid = event.getPlayer().getUniqueId();
        enabled.remove(uuid);
        chosenMouths.remove(uuid);
        positions.remove(uuid);
    }

    private void broadcast(String channel, byte[] payload) {
        for (Player p : Bukkit.getOnlinePlayers()) {
            if (p.getListeningPluginChannels().contains(channel)) {
                p.sendPluginMessage(this, channel, payload);
            }
        }
    }

    private static byte[] prependUuid(UUID uuid, byte[] body) {
        ByteBuffer buf = ByteBuffer.allocate(16 + body.length);
        buf.putLong(uuid.getMostSignificantBits());
        buf.putLong(uuid.getLeastSignificantBits());
        buf.put(body);
        return buf.array();
    }

    private static byte[] encodeVarString(String s) {
        byte[] utf8 = s.getBytes(StandardCharsets.UTF_8);
        ByteArrayOutputStream out = new ByteArrayOutputStream(utf8.length + 3);
        int len = utf8.length;
        while ((len & ~0x7F) != 0) {
            out.write((len & 0x7F) | 0x80);
            len >>>= 7;
        }
        out.write(len);
        out.write(utf8, 0, utf8.length);
        return out.toByteArray();
    }

    private static String decodeVarString(byte[] data) {
        if (data == null || data.length < 2 || data.length > 260) return null;
        int len = 0, shift = 0, pos = 0;
        while (pos < data.length && shift <= 21) {
            int b = data[pos++] & 0xFF;
            len |= (b & 0x7F) << shift;
            if ((b & 0x80) == 0) {
                if (len <= 0 || len > 256 || pos + len != data.length) return null;
                String s = new String(data, pos, len, StandardCharsets.UTF_8);
                return s.isBlank() ? null : s;
            }
            shift += 7;
        }
        return null;
    }

    private static boolean isValidPositionPayload(byte[] data) {
        if (data == null || data.length != 8) return false;
        ByteBuffer buf = ByteBuffer.wrap(data);
        return Float.isFinite(buf.getFloat()) && Float.isFinite(buf.getFloat());
    }
}
