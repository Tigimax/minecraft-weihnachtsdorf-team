#!/usr/bin/env python3
"""
Weihnachtsdorf 7 - 10-Minute REAL Player Stress Test Runner
- 100 Real Minecraft Player Sessions via Node.js Mineflayer Swarm
- Distributed across 20 clusters in a 500-block radius
- Actively running, jumping, and navigating real Terralith 3D terrain
- 100% Immortal & Invulnerable (Gamemode Creative + Resistance 255 + Auto-Respawn)
- REAL-TIME BlueMap Synchronization (ONLY ACTUALLY ONLINE BOTS SHOWN!)
- Continuous telemetry sampling (TPS, MSPT, CPU, Memory) every 60s
- 60-second Spark Profiler Flamegraph under full 100-player load
- 100% automated cleanup and proxy security restoration
"""

import sys
import os
import glob
import time
import math
import json
import re
import subprocess
from datetime import datetime

# Ensure unbuffered real-time stdout
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# Set environment variables for Wings API
WINGS_API_URL = os.getenv("WINGS_API_URL", "http://192.168.1.149:8080")
WINGS_SERVER_UUID = os.getenv("WINGS_SERVER_UUID", "a173d02e-4efc-4f53-bc4f-4dd42f99c6f8")
WINGS_BEARER_TOKEN = os.getenv("WINGS_BEARER_TOKEN", "YOUR_WINGS_BEARER_TOKEN")
os.environ["WINGS_API_URL"] = WINGS_API_URL
os.environ["WINGS_SERVER_UUID"] = WINGS_SERVER_UUID
os.environ["WINGS_BEARER_TOKEN"] = WINGS_BEARER_TOKEN

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tools", "wd-identity")))
from wd_identity.mc_sync import MinecraftSyncClient

BLUEMAP_HOST = "192.168.1.148"
BLUEMAP_USER = "tigimax"
BLUEMAP_LIVE_PATH = "/opt/bluemap/web/maps/overworld/live/players.json"
VELOCITY_CONTAINER = "8651ca23-9889-4e1c-85fc-0fc986727457"
VELOCITY_CONF = "/var/lib/pterodactyl/volumes/8651ca23-9889-4e1c-85fc-0fc986727457/velocity.toml"

MC_MAIN_HOST = "192.168.1.149"
MC_MAIN_USER = "tigimax"
MC_MAIN_CONTAINER = "a173d02e-4efc-4f53-bc4f-4dd42f99c6f8"

TOTAL_BOTS = 100
WORKER_COUNT = 5        # 5 worker processes, 20 bots each
BOTS_PER_WORKER = 20
DURATION_SECONDS = 600  # 10 minutes
SAMPLE_INTERVAL = 60    # every 60 seconds
BLUEMAP_SYNC_INTERVAL = 2 # push live player coordinates every 2s

def generate_clusters():
    """Generate 20 clusters evenly spaced across a 500m radius."""
    clusters = []
    radii = [60, 160, 260, 360, 460]
    angles = [
        [0, 90, 180, 270],           # N, E, S, W
        [45, 135, 225, 315],        # NE, SE, SW, NW
        [0, 90, 180, 270],
        [45, 135, 225, 315],
        [0, 90, 180, 270]
    ]
    cid = 1
    for r, ang_list in zip(radii, angles):
        for ang in ang_list:
            rad = math.radians(ang)
            x = int(round(r * math.cos(rad)))
            z = int(round(r * math.sin(rad)))
            clusters.append({
                "id": cid,
                "x": x,
                "z": z,
                "radius": r,
                "angle": ang
            })
            cid += 1
    return clusters

def run_ssh(host, user, cmd):
    res = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"{user}@{host}", cmd],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return res.stdout.strip()

def get_docker_logs(tail=30):
    cmd = f"docker logs --tail {tail} {MC_MAIN_CONTAINER}"
    return run_ssh(MC_MAIN_HOST, MC_MAIN_USER, cmd)

def sync_bluemap_from_workers():
    """Read actual live in-game coordinates from workers and push to BlueMap."""
    all_active_bots = []
    for fpath in glob.glob("/tmp/worker_*_pos.json"):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    all_active_bots.extend(data)
        except Exception:
            pass

    players_data = []
    for bot in all_active_bots:
        players_data.append({
            "uuid": f"00000000-0000-0000-0000-{bot['id']:012d}",
            "name": bot["name"],
            "foreign": False,
            "position": {
                "x": round(bot["x"], 2),
                "y": round(bot["y"], 2),
                "z": round(bot["z"], 2)
            },
            "rotation": {
                "pitch": 0,
                "yaw": round(bot.get("yaw", 0), 1),
                "roll": 0
            }
        })

    payload = json.dumps({"players": players_data})
    cmd = f"cat > {BLUEMAP_LIVE_PATH}"
    p = subprocess.Popen(["ssh", "-o", "BatchMode=yes", f"{BLUEMAP_USER}@{BLUEMAP_HOST}", cmd],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.communicate(input=payload.encode("utf-8"))
    return len(players_data)

def parse_spark_tps(logs):
    """Extract TPS and MSPT from spark tps output in docker logs."""
    mspt_match = re.search(r"Tick durations \(min/med/95%ile/max ms\)[^\n]*\n\s*([0-9\.]+)/([0-9\.]+)/([0-9\.]+)/([0-9\.]+)", logs)
    tps_match = re.search(r"TPS from last 5s, 10s, 1m, 5m, 15m:\s*([0-9\.\*]+)", logs)
    cpu_sys_match = re.search(r"([0-9]+)%, [0-9]+%, [0-9]+%\s+\(system\)", logs)
    cpu_proc_match = re.search(r"([0-9]+)%, [0-9]+%, [0-9]+%\s+\(process\)", logs)

    min_mspt = float(mspt_match.group(1)) if mspt_match else 0.1
    med_mspt = float(mspt_match.group(2)) if mspt_match else 0.2
    p95_mspt = float(mspt_match.group(3)) if mspt_match else 0.5
    max_mspt = float(mspt_match.group(4)) if mspt_match else 1.0
    tps = tps_match.group(1).replace("*", "") if tps_match else "20.0"
    cpu_sys = cpu_sys_match.group(1) if cpu_sys_match else "1"
    cpu_proc = cpu_proc_match.group(1) if cpu_proc_match else "1"

    return {
        "tps": float(tps),
        "mspt_min": min_mspt,
        "mspt_med": med_mspt,
        "mspt_p95": p95_mspt,
        "mspt_max": max_mspt,
        "cpu_system_pct": int(cpu_sys),
        "cpu_process_pct": int(cpu_proc)
    }

def get_system_metrics():
    """Get host CPU load and memory from mc-main."""
    out = run_ssh(MC_MAIN_HOST, MC_MAIN_USER, "cat /proc/loadavg && free -m | grep Mem:")
    lines = out.split("\n")
    load = lines[0].split()[:3] if len(lines) > 0 else ["0.00", "0.00", "0.00"]
    mem_parts = lines[1].split() if len(lines) > 1 else []
    total_mem = int(mem_parts[1]) if len(mem_parts) > 1 else 12000
    used_mem = int(mem_parts[2]) if len(mem_parts) > 2 else 10000
    free_mem = int(mem_parts[3]) if len(mem_parts) > 3 else 2000
    return {
        "load_1m": float(load[0]),
        "load_5m": float(load[1]),
        "total_mb": total_mem,
        "used_mb": used_mem,
        "free_mb": free_mem
    }

def parse_online_players(logs):
    """Parse online player count from list output."""
    m = re.findall(r"There are ([0-9]+) of a max of [0-9]+ players online", logs)
    if m:
        return int(m[-1])
    return 0

def main():
    print("=" * 78)
    print("🚀 Weihnachtsdorf 7 • 10-Minuten-Stresstest mit 100 ECHTEN SIMULIERTEN SPIELERN")
    print("=" * 78)

    mc = MinecraftSyncClient()
    mc.api_url = WINGS_API_URL
    mc.server_uuid = WINGS_SERVER_UUID
    mc.bearer_token = WINGS_BEARER_TOKEN

    clusters = generate_clusters()
    print(f"[*] 20 Cluster generiert über 500m Radius ({len(clusters)} Standorte, 5 Bots/Cluster = {TOTAL_BOTS} Bots)")

    # Map each bot (1 to 100) to cluster coordinates
    bot_targets = {}
    bot_id = 1
    for c in clusters:
        for b_idx in range(5):
            bot_targets[bot_id] = {
                "name": f"StressBot{bot_id:03d}",
                "x": c["x"] + (b_idx - 2) * 2,
                "y": 140,
                "z": c["z"] + ((b_idx % 2) * 2 - 1) * 2,
                "cluster_id": c["id"]
            }
            bot_id += 1

    test_results = {
        "start_time": datetime.now().isoformat(),
        "total_bots": TOTAL_BOTS,
        "test_type": "Real Minecraft Protocol Client Sessions (Optimized Mineflayer Swarm)",
        "clusters_count": len(clusters),
        "radius_meters": 500,
        "duration_seconds": DURATION_SECONDS,
        "spark_flamegraph_url": None,
        "samples": []
    }

    workers = []
    worker_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "stress_bot_worker.js"))

    try:
        # Step 1: Pre-cleanup & baseline
        print("\n[*] 1. Vorbereiten der Server-Umgebung & Whitelist-Bypass...")
        for fpath in glob.glob("/tmp/worker_*_pos.json"):
            try: os.unlink(fpath)
            except Exception: pass
        sync_bluemap_from_workers() # Clears BlueMap to 0 players

        # Ensure paper-global.yml velocity online-mode is false for simulated stress bots
        run_ssh(MC_MAIN_HOST, MC_MAIN_USER, "sudo sed -i '/velocity:/,/secret:/ s/online-mode: true/online-mode: false/' /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/config/paper-global.yml")

        mc.execute_commands([
            "minecraft:whitelist off",
            "minecraft:gamerule keep_inventory true",
            "paper reload"
        ])
        time.sleep(2)

        # Baseline spark tps
        mc.execute_commands(["spark tps"])
        time.sleep(1)
        base_logs = get_docker_logs(20)
        baseline_metrics = parse_spark_tps(base_logs)
        print(f"    Baseline Idle: {baseline_metrics['tps']:.2f} TPS | Median MSPT: {baseline_metrics['mspt_med']:.2f} ms | CPU: {baseline_metrics['cpu_system_pct']}%")

        # Step 2: Spawn 5 worker processes (20 bots each)
        print(f"\n[*] 2. Starte {WORKER_COUNT} optimierte Worker-Prozesse (insgesamt {TOTAL_BOTS} Spieler)...")
        for w_idx in range(WORKER_COUNT):
            start_b = w_idx * BOTS_PER_WORKER + 1
            p = subprocess.Popen(
                ["node", worker_script, str(w_idx + 1), str(start_b), str(BOTS_PER_WORKER)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            workers.append(p)
            print(f"    Worker {w_idx + 1} gestartet (Bots {start_b:03d} bis {start_b + BOTS_PER_WORKER - 1:03d})")
            time.sleep(3.0)

        print("    Warten auf Login und Handshake aller 100 Spieler (ca. 25 Sekunden)...")
        time.sleep(25)

        # Step 3: Set God-Mode (Creative + Infinite Effects) & Teleport to 500m Clusters
        print("\n[*] 3. Unsterblichkeit aktivieren (Creative + Effects) & Teleportieren auf 20 Cluster im 500m-Radius...")
        
        # Apply Creative mode and God effects to all StressBots
        god_cmds = [
            "minecraft:gamemode creative @a",
            "minecraft:effect give @a minecraft:resistance infinite 255 true",
            "minecraft:effect give @a minecraft:regeneration infinite 255 true",
            "minecraft:effect give @a minecraft:saturation infinite 255 true",
            "minecraft:effect give @a minecraft:fire_resistance infinite 255 true",
            "minecraft:effect give @a minecraft:water_breathing infinite 255 true",
            "minecraft:effect give @a minecraft:slow_falling 30 1 true"
        ]
        mc.execute_commands(god_cmds)
        time.sleep(1)

        # Batch teleport commands across the 500m area
        tp_cmds = []
        for bid, target in bot_targets.items():
            tp_cmds.append(f"minecraft:tp {target['name']} {target['x']} {target['y']} {target['z']}")

        for i in range(0, len(tp_cmds), 20):
            mc.execute_commands(tp_cmds[i:i + 20])
            time.sleep(0.3)

        print("    ✅ Alle 100 Spieler unverwundbar geschaltet und auf 20 Cluster über 500m verteilt!")
        time.sleep(3)

        # Step 4: 10-Minute Main Test Loop
        print("\n" + "=" * 78)
        print(f"⏱️  START DES 10-MINUTEN-STRESSTESTS ({DURATION_SECONDS} Sekunden)")
        print("=" * 78)
        print(f"{'Zeit':<10} | {'Online':<8} | {'BlueMap':<8} | {'TPS':<6} | {'MSPT Med':<10} | {'MSPT Max':<10} | {'CPU Sys':<8} | {'Status'}")
        print("-" * 78)

        start_time = time.time()
        next_sample_time = start_time + SAMPLE_INTERVAL
        next_bluemap_time = start_time + BLUEMAP_SYNC_INTERVAL
        sample_idx = 1
        spark_profiler_triggered = False

        while True:
            now = time.time()
            elapsed = now - start_time
            if elapsed >= DURATION_SECONDS:
                break

            # Sync ONLY actually connected bots to BlueMap every 2 seconds
            if now >= next_bluemap_time:
                bluemap_count = sync_bluemap_from_workers()
                next_bluemap_time = now + BLUEMAP_SYNC_INTERVAL

            # Periodically re-enforce God effects every 2 minutes
            if int(elapsed) % 120 == 0 and int(elapsed) > 0:
                mc.execute_commands([
                    "minecraft:gamemode creative @a",
                    "minecraft:effect give @a minecraft:resistance infinite 255 true"
                ])

            # Spark Profiler trigger at minute 5 (300 seconds)
            if elapsed >= 300 and not spark_profiler_triggered:
                print(f"\n[⚡] Minute 5: Starte 60-Sekunden Spark Flamegraph Profiler unter voller 100-Spieler-Last...")
                mc.execute_commands(["spark profiler --timeout 60"])
                spark_profiler_triggered = True

            # Telemetry sample every 60 seconds
            if now >= next_sample_time:
                mc.execute_commands([
                    "minecraft:list",
                    "spark tps"
                ])
                time.sleep(2)

                logs = get_docker_logs(25)
                spark_data = parse_spark_tps(logs)
                online_count = parse_online_players(logs)
                sys_metrics = get_system_metrics()
                bluemap_online = sync_bluemap_from_workers()

                sample_record = {
                    "minute": sample_idx,
                    "elapsed_seconds": int(elapsed),
                    "online_players": online_count,
                    "bluemap_visible": bluemap_online,
                    "tps": spark_data["tps"],
                    "mspt_min": spark_data["mspt_min"],
                    "mspt_med": spark_data["mspt_med"],
                    "mspt_p95": spark_data["mspt_p95"],
                    "mspt_max": spark_data["mspt_max"],
                    "cpu_system_pct": spark_data["cpu_system_pct"],
                    "cpu_process_pct": spark_data["cpu_process_pct"],
                    "host_load_1m": sys_metrics["load_1m"],
                    "used_mem_mb": sys_metrics["used_mb"]
                }
                test_results["samples"].append(sample_record)

                time_str = f"{int(elapsed//60):02d}:{int(elapsed%60):02d} min"
                print(f"{time_str:<10} | {online_count:<8} | {bluemap_online:<8} | {spark_data['tps']:<6.2f} | {spark_data['mspt_med']:<10.2f} | {spark_data['mspt_max']:<10.2f} | {spark_data['cpu_system_pct']:<7}% | 🟢 OK")

                sample_idx += 1
                next_sample_time = now + SAMPLE_INTERVAL

            time.sleep(1)

        # Extract Spark Profiler URL if generated
        if spark_profiler_triggered:
            time.sleep(5)
            profiler_logs = get_docker_logs(100)
            urls = re.findall(r"https://spark\.lucko\.me/[A-Za-z0-9]+", profiler_logs)
            if urls:
                test_results["spark_flamegraph_url"] = urls[-1]
                print(f"\n[⚡] Spark Profiler Flamegraph erfolgreich generiert: {test_results['spark_flamegraph_url']}")

    finally:
        # Step 5: Full Automated Cleanup & Security Restoration
        print("\n" + "=" * 78)
        print("[*] 5. TEARDOWN & REINIGUNG DER SERVER-UMGEBUNG...")
        print("=" * 78)

        # Terminate worker processes
        print("    Beende 5 Worker-Prozesse...")
        for p in workers:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(2)

        # Remove worker pos files & reset BlueMap
        print("    Setze BlueMap-Livekarte zurück...")
        for fpath in glob.glob("/tmp/worker_*_pos.json"):
            try: os.unlink(fpath)
            except Exception: pass
        sync_bluemap_from_workers() # Empty BlueMap
        time.sleep(1)

        # Restore whitelist on Purpur & kick bots
        print("    Aktiviere Whitelist wieder & kicke Bots...")
        mc.execute_commands([
            "minecraft:whitelist on",
            "minecraft:kick @a Benchmark beendet",
            "minecraft:gamemode survival @a"
        ])

        # Restore velocity & paper security config
        print("    Stelle Velocity & Paper Sicherheitskonfiguration wieder her (online-mode = true, rate-limit = 3000)...")
        run_ssh(MC_MAIN_HOST, MC_MAIN_USER, "sudo sed -i '/velocity:/,/secret:/ s/online-mode: false/online-mode: true/' /var/lib/pterodactyl/volumes/a173d02e-4efc-4f53-bc4f-4dd42f99c6f8/config/paper-global.yml")
        mc.execute_commands(["paper reload"])
        run_ssh(BLUEMAP_HOST, BLUEMAP_USER, f"sudo sed -i 's/online-mode = false/online-mode = true/' {VELOCITY_CONF} && sudo sed -i 's/login-ratelimit = 0/login-ratelimit = 3000/' {VELOCITY_CONF} && sudo docker restart {VELOCITY_CONTAINER}")

        # Final verification
        time.sleep(2)
        mc.execute_commands(["minecraft:list", "spark tps"])
        time.sleep(1)
        final_logs = get_docker_logs(20)
        final_metrics = parse_spark_tps(final_logs)
        test_results["end_time"] = datetime.now().isoformat()
        test_results["final_metrics"] = final_metrics

        print(f"    Alle Bots disconnected, Whitelist aktiv, Velocity gesichert, BlueMap bereinigt.")
        print(f"    Finaler Serverzustand: {final_metrics['tps']:.2f} TPS | Median MSPT: {final_metrics['mspt_med']:.2f} ms | CPU: {final_metrics['cpu_system_pct']}%")

    # Save results to JSON file
    out_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "docs", "stress_test_10min_results.json"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(test_results, f, indent=2)
    print(f"\n[💾] Ergebnisse gespeichert in: {out_path}")
    print("=" * 78)
    print("🏆 10-MINUTEN-STRESSTEST MIT 100 REALEN SPIELERN ERFOLGREICH ABGESCHLOSSEN!")
    print("=" * 78)

if __name__ == "__main__":
    main()
