import time
import sys
from wd_identity.mc_sync import MinecraftSyncClient

mc = MinecraftSyncClient()
cmd = 'summon zombified_piglin 100 310 100 {Tags:["test_walker"],CustomName:\'{"text":"StressBot-Test","color":"gold"}\',CustomNameVisible:1b,Invulnerable:1b,PersistenceRequired:1b,Attributes:[{Name:"generic.movement_speed",Base:0.35f}]}'

print("1. Force loading chunk [100, 100] and summoning bot...")
res = mc.execute_commands(["forceload add 100 100", cmd])
print(f"Spawn command result: {res}")

time.sleep(3)
mc.execute_commands(["execute in minecraft:overworld run data get entity @e[tag=test_walker,limit=1] Pos"])

time.sleep(3)
mc.execute_commands(["execute in minecraft:overworld run data get entity @e[tag=test_walker,limit=1] Pos"])

time.sleep(2)
mc.execute_commands(["minecraft:kill @e[tag=test_walker]", "forceload remove all"])
print("Test completed and cleaned up.")
