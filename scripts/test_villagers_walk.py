import time
from wd_identity.mc_sync import MinecraftSyncClient

mc = MinecraftSyncClient()
cmds = ["forceload add 150 150"]
for i in range(5):
    cmds.append(f'summon villager 150 120 150 {{Tags:["v_stress","bot_{i}"],CustomName:\'{{"text":"StressBot-{i}"}}\',CustomNameVisible:1b,Invulnerable:1b,PersistenceRequired:1b,Attributes:[{{Name:"generic.movement_speed",Base:0.40f}}]}}')

mc.execute_commands(cmds)
print("Spawned 5 villagers. Waiting 20 seconds...")
time.sleep(20)

for i in range(5):
    mc.execute_commands([f'execute in minecraft:overworld run data get entity @e[tag=bot_{i},limit=1] Pos'])

time.sleep(2)
mc.execute_commands(['minecraft:kill @e[tag=v_stress]', 'forceload remove all'])
print("Done test.")
