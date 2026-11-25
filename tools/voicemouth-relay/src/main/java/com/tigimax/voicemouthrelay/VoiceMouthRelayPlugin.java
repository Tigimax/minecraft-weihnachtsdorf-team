package com.tigimax.voicemouthrelay;

import org.bukkit.Bukkit;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerQuitEvent;
import org.bukkit.event.player.PlayerRegisterChannelEvent;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.plugin.messaging.PluginMessageListener;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

public final class VoiceMouthRelayPlugin extends JavaPlugin implements PluginMessageListener, Listener {

    private static final String SELECT_MOUTH_C2S = "voicemouth:select_mouth_c2s";
    private static final String SELECT_POS_C2S   = "voicemouth:select_position_c2s";
    private static final String SYNC_MOUTH_S2C   = "voicemouth:sync_mouth_s2c";
    private static final String SYNC_POS_S2C     = "voicemouth:sync_position_s2c";

    private final Map<UUID, byte[]> mouths = new ConcurrentHashMap<>();
    private final Map<UUID, byte[]> positions = new ConcurrentHashMap<>();

    @Override
    public void onEnable() {
        var messenger = getServer().getMessenger();
        messenger.registerIncomingPluginChannel(this, SELECT_MOUTH_C2S, this);
        messenger.registerIncomingPluginChannel(this, SELECT_POS_C2S, this);
        messenger.registerOutgoingPluginChannel(this, SYNC_MOUTH_S2C);
        messenger.registerOutgoingPluginChannel(this, SYNC_POS_S2C);
        getServer().getPluginManager().registerEvents(this, this);
        getLogger().info("VoiceMouthRelay enabled.");
    }

    @Override
    public void onDisable() {
        getServer().getMessenger().unregisterIncomingPluginChannel(this);
        getServer().getMessenger().unregisterOutgoingPluginChannel(this);
        mouths.clear();
        positions.clear();
    }

    @Override
    public void onPluginMessageReceived(String channel, Player sender, byte[] message) {
        UUID uuid = sender.getUniqueId();
        if (SELECT_MOUTH_C2S.equals(channel)) {
            if (!isValidMouthPayload(message)) return;
            mouths.put(uuid, message.clone());
            byte[] out = prependUuid(uuid, message);
            broadcast(SYNC_MOUTH_S2C, out);
        } else if (SELECT_POS_C2S.equals(channel)) {
            if (!isValidPositionPayload(message)) return;
            positions.put(uuid, message.clone());
            byte[] out = prependUuid(uuid, message);
            broadcast(SYNC_POS_S2C, out);
        }
    }

    @EventHandler
    public void onChannelRegister(PlayerRegisterChannelEvent event) {
        Player p = event.getPlayer();
        if (SYNC_MOUTH_S2C.equals(event.getChannel())) {
            mouths.forEach((uuid, raw) -> p.sendPluginMessage(this, SYNC_MOUTH_S2C, prependUuid(uuid, raw)));
        } else if (SYNC_POS_S2C.equals(event.getChannel())) {
            positions.forEach((uuid, raw) -> p.sendPluginMessage(this, SYNC_POS_S2C, prependUuid(uuid, raw)));
        }
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent event) {
        UUID uuid = event.getPlayer().getUniqueId();
        mouths.remove(uuid);
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

    // Minecraft VarInt-prefixed UTF-8 string (max 256 bytes)
    private static boolean isValidMouthPayload(byte[] data) {
        if (data == null || data.length < 2 || data.length > 260) return false;
        int len = 0, shift = 0, pos = 0;
        while (pos < data.length && shift <= 21) {
            int b = data[pos++] & 0xFF;
            len |= (b & 0x7F) << shift;
            if ((b & 0x80) == 0) {
                return len > 0 && len <= 256 && pos + len == data.length
                        && !new String(data, pos, len, StandardCharsets.UTF_8).isBlank();
            }
            shift += 7;
        }
        return false;
    }

    // Two big-endian finite IEEE-754 floats (offsetX, offsetY)
    private static boolean isValidPositionPayload(byte[] data) {
        if (data == null || data.length != 8) return false;
        ByteBuffer buf = ByteBuffer.wrap(data);
        float x = buf.getFloat();
        float y = buf.getFloat();
        return Float.isFinite(x) && Float.isFinite(y);
    }
}
