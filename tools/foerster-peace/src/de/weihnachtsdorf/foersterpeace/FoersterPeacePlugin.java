package de.weihnachtsdorf.foersterpeace;

import org.bukkit.Bukkit;
import org.bukkit.World;
import org.bukkit.entity.Entity;
import org.bukkit.entity.Mob;
import org.bukkit.entity.Player;
import org.bukkit.entity.Projectile;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.EntityDamageByEntityEvent;
import org.bukkit.event.entity.EntityTargetEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * Prevents mobs from targeting or hurting Waldlaufer_Rudi so he lives peacefully among them.
 */
public class FoersterPeacePlugin extends JavaPlugin implements Listener {
    private static final String BOT_NAME = "Waldlaufer_Rudi";

    @Override
    public void onEnable() {
        Bukkit.getPluginManager().registerEvents(this, this);
        clearExistingBotTargets();
        getLogger().info("FoersterPeace aktiv: Mobs lassen " + BOT_NAME + " in Ruhe.");
    }

    private boolean isProtectedBot(Entity entity) {
        return entity instanceof Player player && BOT_NAME.equalsIgnoreCase(player.getName());
    }

    private void clearExistingBotTargets() {
        for (World world : Bukkit.getWorlds()) {
            for (Mob mob : world.getEntitiesByClass(Mob.class)) {
                if (isProtectedBot(mob.getTarget())) {
                    mob.setTarget(null);
                }
            }
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onEntityTarget(EntityTargetEvent event) {
        if (isProtectedBot(event.getTarget())) {
            event.setTarget(null);
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onEntityDamageByEntity(EntityDamageByEntityEvent event) {
        if (!isProtectedBot(event.getEntity())) {
            return;
        }
        Entity damager = event.getDamager();
        if (damager instanceof Player) {
            return;
        }
        if (damager instanceof Projectile projectile && projectile.getShooter() instanceof Player) {
            return;
        }
        event.setCancelled(true);
        if (damager instanceof Mob mob && isProtectedBot(mob.getTarget())) {
            mob.setTarget(null);
        }
    }

    @EventHandler
    public void onPlayerJoin(PlayerJoinEvent event) {
        if (isProtectedBot(event.getPlayer())) {
            clearExistingBotTargets();
        }
    }
}
