package de.weihnachtsdorf.botauth;

import com.velocitypowered.api.event.Subscribe;
import com.velocitypowered.api.event.connection.PreLoginEvent;
import com.velocitypowered.api.event.player.GameProfileRequestEvent;
import com.velocitypowered.api.util.GameProfile;
import net.kyori.adventure.text.Component;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class LocalBotAuth {

    private static final Logger LOGGER = LoggerFactory.getLogger("wd-bot-auth");
    private static final String BOT_NAME = "Waldlaufer_Rudi";

    private static boolean isInternalLanAddress(String ip) {
        if (ip == null || ip.endsWith(".174")) {
            return false;
        }
        return ip.startsWith("127.") || ip.startsWith("192.168.") || ip.startsWith("10.");
    }

    // Mojang-signed Forester/Lumberjack skin texture
    private static final String SKIN_VALUE =
        "ewogICJ0aW1lc3RhbXAiIDogMTc5NDU3MzkyMjU0MywKICAicHJvZmlsZUlkIiA6ICI3NzQxMDg0NTVlYTM0ZjRlODJiZjgxYzg5NDYxMWU2MSIsCiAgInByb2ZpbGVOYW1lIiA6ICJsdW1iZXJqYWNrIiwKICAic2lnbmF0dXJlUmVxdWlyZWQiIDogdHJ1ZSwKICAidGV4dHVyZXMiIDogewogICAgIlNLSU4iIDogewogICAgICAidXJsIiA6ICJodHRwOi8vdGV4dHVyZXMubWluZWNyYWZ0Lm5ldC90ZXh0dXJlL2U2NjFmMzJjNmNmMDFhZjg2N2RjNWIyMzEyZGEyMDFlYmRlNDU2YWE0MmY5NDA1ZjU5MmU0ZGJhYTI5ODEwOTkiCiAgICB9CiAgfQp9";

    private static final String SKIN_SIG =
        "SdGGyHu6eIFzs+vezTQzMsw/IsoTByp3eLoqaDHAqtuG8epeef8ZXOwSoEyAL+SOabpzi4KtNXzciQXvqP9o02EejODim3oS9dwpkERGogzENlZ8u4kL1NAv0dX++Rx2+uLjjUemkO5VSgGrfD7wAUgyXI0G/7af1QXFU91RGSFUiPBoge+/QSZMVZ1NggkXA3PsvVzZFUpcrAYEJWbvEgc6wEV8BaqcCcFSzbpH8r1PrNX7boybSUDRafpoIH9GjO9abPKqr/CDc3lLVM1mCzu4zpba/PYeUwHB5S2qbjJf1H7r6UHbvwq89Vq4kXrE9IoF8TDAAc/BzR67LinfeP/ao7Br8UbRntRiqp95N5+sBe3aI5P7n3I0bDcespoaq/756XCPWz89TD/MZFWJV5NLRT6fI5CI+2f/kVNpxc4sjE+x6NhDutavFUIAqAPI0EnZp42lewPw4zoNFcvIkk7DmA9jzMNc00CS5nrQ+GI5i4MuXXtl/ZtTN6EYFkRJ6Qo1fdCOqO7yphrVzFz6e/b1uEd2iFiZWAYBdA62M9biRMa2VRzkz/MldxxNW2zGHXEBa/rgcublQ5j0XMqAVYjUQFeWhrFd3q8BLPpar9o53qNq+VqehjqaK3vo82ZANmjPbDyz5PptxDXwlrMm1NUUV898rQYyeWg8neFtlJ8=";

    @Subscribe
    public void onPreLogin(PreLoginEvent event) {
        if (!BOT_NAME.equalsIgnoreCase(event.getUsername())) {
            return;
        }
        String ip = event.getConnection().getRemoteAddress().getAddress().getHostAddress();
        if (isInternalLanAddress(ip)) {
            event.setResult(PreLoginEvent.PreLoginComponentResult.forceOfflineMode());
        } else {
            event.setResult(PreLoginEvent.PreLoginComponentResult.denied(
                Component.text("Dieser Server-Bot-Account darf nur intern verbunden werden.")
            ));
        }
    }

    @Subscribe
    public void onGameProfileRequest(GameProfileRequestEvent event) {
        if (!BOT_NAME.equalsIgnoreCase(event.getUsername())) {
            return;
        }
        GameProfile base = GameProfile.forOfflinePlayer(BOT_NAME);
        GameProfile withSkin = base.addProperty(new GameProfile.Property("textures", SKIN_VALUE, SKIN_SIG));
        event.setGameProfile(withSkin);
        LOGGER.info("Applied Mojang-signed lumberjack skin textures to {} (uuid={})", BOT_NAME, base.getId());
    }
}
