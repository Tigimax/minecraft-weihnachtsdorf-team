import time
from wd_identity.mc_sync import MinecraftSyncClient

mc = MinecraftSyncClient()
mobs = [
    ("zombified_piglin", "test_zp"),
    ("pillager", "test_pil"),
    ("vindicator", "test_vind"),
    ("villager", "test_vil"),
    ("witch", "test_wtc")
]

cmds = ["forceload add 0 0"]
for mob, tag in mobs:
    cmds.append(f'summon {mob} 0 100 0 {{Tags:["{tag}","stress_test_type"],CustomName:\'{{"text":"{tag}"}}\',Invulnerable:1b,PersistenceRequired:1b}}')

mc.execute_commands(cmds)
print("Spawned mobs. Waiting 15 seconds...")
time.sleep(15)

for mob, tag in mobs:
    mc.execute_commands([f'execute in minecraft:overworld run data get entity @e[tag={tag},limit=1] Pos'])

time.sleep(2)
mc.execute_commands(['minecraft:kill @e[tag=stress_test_type]', 'forceload remove all'])
print("Done test.")
