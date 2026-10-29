import time
from wd_identity.mc_sync import MinecraftSyncClient

mc = MinecraftSyncClient()
cmds = [
    'forceload add 100 100',
    'summon zombified_piglin 100 250 100 {Tags:["wanderer"],CustomName:\'{"text":"Wanderer1"}\',CustomNameVisible:1b,Invulnerable:1b,PersistenceRequired:1b,Attributes:[{Name:"generic.movement_speed",Base:0.40f}]}'
]
mc.execute_commands(cmds)
print("Spawned. Waiting 6 seconds for landing...")
time.sleep(6)
mc.execute_commands(['execute in minecraft:overworld run data get entity @e[tag=wanderer,limit=1] Pos'])
print("Waiting 10 seconds for wandering...")
time.sleep(10)
mc.execute_commands(['execute in minecraft:overworld run data get entity @e[tag=wanderer,limit=1] Pos'])
time.sleep(2)
mc.execute_commands(['minecraft:kill @e[tag=wanderer]', 'forceload remove all'])
print("Cleaned up.")
