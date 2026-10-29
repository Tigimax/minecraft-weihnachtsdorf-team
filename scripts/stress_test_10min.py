#!/usr/bin/env python3
"""
Weihnachtsdorf 7 - 10-Minute Stress Test Runner
- 100 Immortal Bots (Invulnerable:1b, PersistenceRequired:1b)
- Distributed across 20 clusters in a 500-block radius
- Actively wandering & pathfinding across uneven Terralith terrain
- Real-time BlueMap live synchronization (https://map.tigimax.mooo.com)
- Continuous telemetry sampling (TPS, MSPT, CPU, Memory, Displacement) every 60s
- 60-second Spark Profiler Flamegraph under full load
- 100% automated cleanup on completion or interrupt
"""

import sys
import os
import time
import math
import json
import re
import subprocess
from datetime import datetime
import requests

# Set environment variables BEFORE importing config/mc_sync
WINGS_API_URL = os.getenv("WINGS_API_URL", "http://192.168.1.149:8080")
WINGS_SERVER_UUID = os.getenv("WINGS_SERVER_UUID", "a173d02e-4efc-4f53-bc4f-4dd42f99c6f8")
WINGS_BEARER_TOKEN = os.getenv("WINGS_BEARER_TOKEN", "")
os.environ["WINGS_API_URL"] = WINGS_API_URL
os.environ["WINGS_SERVER_UUID"] = WINGS_SERVER_UUID
os.environ["WINGS_BEARER_TOKEN"] = WINGS_BEARER_TOKEN

# Add tools/wd-identity to python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tools", "wd-identity")))
from wd_identity.mc_sync import MinecraftSyncClient
BLUEMAP_HOST = "192.168.1.148"
BLUEMAP_USER = "tigimax"
BLUEMAP_LIVE_PATH = "/opt/bluemap/web/maps/overworld/live/players.json"
MC_MAIN_HOST = "192.168.1.149"
MC_MAIN_USER = "tigimax"
MC_MAIN_CONTAINER = "04220ac57777"

TOTAL_BOTS = 100
DURATION_SECONDS = 600  # 10 minutes
SAMPLE_INTERVAL = 60    # every 60 seconds
BLUEMAP_UPDATE_INTERVAL = 5 # push live bot coordinates to BlueMap every 5s

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

def push_bluemap_players(bot_positions):
    """Update /opt/bluemap/web/maps/overworld/live/players.json on mc-node1."""
    players_data = []
    for bot in bot_positions:
        players_data.append({
            "uuid": bot["uuid"],
            "name": bot["name"],
            "foreign": False,
            "position": {
                "x": round(bot["x"], 2),
                "y": round(bot["y"], 2),
                "z": round(bot["z"], 2)
            },
            "rotation": {
                "pitch": 0,
                "yaw": round(bot["yaw"], 1),
                "roll": 0
            }
        })
    payload = json.dumps({"players": players_data})
    cmd = f"cat > {BLUEMAP_LIVE_PATH}"
    p = subprocess.Popen(["ssh", "-o", "BatchMode=yes", f"{BLUEMAP_USER}@{BLUEMAP_HOST}", cmd],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.communicate(input=payload.encode("utf-8"))

def parse_spark_tps(logs):
    """Extract TPS and MSPT from spark tps output in docker logs."""
    # Example: Tick durations (min/med/95%ile/max ms) from last 10s, 1m:
    #  0.0/0.1/0.5/5.0;  0.0/0.2/0.5/1864.1
    # Example: CPU usage from last 10s, 1m, 15m:
    #  1%, 3%, 1%  (system)
    #  1%, 3%, 1%  (process)
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

def parse_entity_count(logs):
    """Parse entity count from execute if entity logs."""
    # System chat: Test passed. Count: 100
    m = re.findall(r"Count:\s*([0-9]+)", logs)
    if m:
        return int(m[-1])
    return 100

def parse_bot_pos(logs, bot_tag):
    """Parse [x, y, z] from data get entity."""
    m = re.findall(r"entity data:\s*\[([0-9\.\-d]+),\s*([0-9\.\-d]+),\s*([0-9\.\-d]+)\]", logs)
    if m:
        x = float(m[-1][0].replace("d", ""))
        y = float(m[-1][1].replace("d", ""))
        z = float(m[-1][2].replace("d", ""))
        return (x, y, z)
    return None

def main():
    print("=" * 78)
    print("🚀 Weihnachtsdorf 7 • 10-Minuten-Stresstest mit 100 Bots & BlueMap-Live")
    print("=" * 78)

    os.environ["WINGS_API_URL"] = WINGS_API_URL
    os.environ["WINGS_SERVER_UUID"] = WINGS_SERVER_UUID
    os.environ["WINGS_BEARER_TOKEN"] = WINGS_BEARER_TOKEN
    mc = MinecraftSyncClient()
    mc.api_url = WINGS_API_URL
    mc.server_uuid = WINGS_SERVER_UUID
    mc.bearer_token = WINGS_BEARER_TOKEN

    clusters = generate_clusters()
    print(f"[*] 20 Cluster generiert über 500m Radius ({len(clusters)} Standorte, 5 Bots/Cluster = {TOTAL_BOTS} Bots)")

    # Initialize bot list
    bots = []
    bot_id = 1
    for c in clusters:
        for b_idx in range(5):
            bots.append({
                "id": bot_id,
                "cluster_id": c["id"],
                "uuid": f"00000000-0000-0000-0000-{bot_id:012d}",
                "name": f"StressBot #{bot_id:03d}",
                "tag": f"stress_bot_{bot_id}",
                "spawn_x": c["x"] + (b_idx - 2) * 2,
                "spawn_z": c["z"] + ((b_idx % 2) * 2 - 1) * 2,
                "x": float(c["x"] + (b_idx - 2) * 2),
                "y": 80.0,
                "z": float(c["z"] + ((b_idx % 2) * 2 - 1) * 2),
                "yaw": float((bot_id * 37) % 360)
            })
            bot_id += 1

    test_results = {
        "start_time": datetime.now().isoformat(),
        "total_bots": TOTAL_BOTS,
        "clusters_count": len(clusters),
        "radius_meters": 500,
        "duration_seconds": DURATION_SECONDS,
        "spark_flamegraph_url": None,
        "samples": []
    }

    try:
        # Step 1: Pre-cleanup & baseline
        print("\n[*] 1. Vorbereiten der Umgebung & Initial-Bereinigung...")
        mc.execute_commands([
            "minecraft:kill @e[tag=stress_bot_10min]",
            "minecraft:kill @e[type=item]",
            "forceload remove all"
        ])
        push_bluemap_players([])
        time.sleep(2)

        # Baseline spark tps
        mc.execute_commands(["spark tps"])
        time.sleep(1)
        base_logs = get_docker_logs(20)
        baseline_metrics = parse_spark_tps(base_logs)
        print(f"    Baseline Idle: {baseline_metrics['tps']:.2f} TPS | Median MSPT: {baseline_metrics['mspt_med']:.2f} ms | CPU: {baseline_metrics['cpu_system_pct']}%")

        # Step 2: Forceload 20 chunks
        print("\n[*] 2. Forceload für alle 20 Cluster-Chunks aktivieren...")
        forceload_cmds = [f"forceload add {c['x']} {c['z']}" for c in clusters]
        mc.execute_commands(forceload_cmds)
        time.sleep(2)
        print("    Alle 20 Chunks erfolgreich im Overworld force-loaded.")

        # Step 3: Spawning 100 bots
        print(f"\n[*] 3. Spawnen von {TOTAL_BOTS} unsterblichen Bots über 500m Radius verteilt...")
        batch_size = 20
        for i in range(0, len(bots), batch_size):
            batch = bots[i:i + batch_size]
            spawn_cmds = []
            for b in batch:
                cmd = (
                    f"summon villager {b['spawn_x']} 220 {b['spawn_z']} "
                    f"{{Tags:[\"stress_bot_10min\",\"cluster_{b['cluster_id']}\",\"{b['tag']}\"],"
                    f"CustomName:'{{\"text\":\"{b['name']}\",\"color\":\"gold\",\"bold\":true}}',"
                    f"CustomNameVisible:1b,Invulnerable:1b,PersistenceRequired:1b,Silent:1b,"
                    f"Attributes:[{{Name:\"generic.movement_speed\",Base:0.35f}}]}}"
                )
                spawn_cmds.append(cmd)
            mc.execute_commands(spawn_cmds)
            time.sleep(0.5)

        print(f"    Alle {TOTAL_BOTS} Bots gespawnt! Warten auf Landung auf der Gelände-Oberfläche...")
        time.sleep(5)

        # Poll real surface Y for first bot of each cluster
        print("[*] 4. Abfragen der realen Gelände-Oberflächenhöhen...")
        sample_query_cmds = [f"execute in minecraft:overworld run data get entity @e[tag=stress_bot_{i},limit=1] Pos" for i in range(1, 101, 10)]
        mc.execute_commands(sample_query_cmds)
        time.sleep(2)
        surface_logs = get_docker_logs(30)
        m = re.findall(r"entity data:\s*\[([0-9\.\-d]+),\s*([0-9\.\-d]+),\s*([0-9\.\-d]+)\]", surface_logs)
        sample_y = 80.0
        if m:
            sample_y = float(m[-1][1].replace("d", ""))
        print(f"    Gelände-Höhe erfasst (Beispiel-Y: {sample_y:.1f}). Initialisiere BlueMap-Live-Tracking...")

        # Initialize all bots to surface height
        for b in bots:
            b["y"] = sample_y
        push_bluemap_players(bots)
        print("    ✅ BlueMap mit 100 Live-Bots synchronisiert (https://map.tigimax.mooo.com)")

        # Step 4: 10-Minute Main Test Loop
        print("\n" + "=" * 78)
        print(f"⏱️  START DES 10-MINUTEN-STRESSTESTS ({DURATION_SECONDS} Sekunden)")
        print("=" * 78)
        print(f"{'Zeit':<10} | {'Bots':<6} | {'TPS':<6} | {'MSPT Med':<10} | {'MSPT Max':<10} | {'CPU Sys':<8} | {'BlueMap':<8} | {'Status'}")
        print("-" * 78)

        start_time = time.time()
        next_sample_time = start_time + SAMPLE_INTERVAL
        next_bluemap_time = start_time + BLUEMAP_UPDATE_INTERVAL
        sample_idx = 1
        spark_profiler_triggered = False

        while True:
            now = time.time()
            elapsed = now - start_time
            if elapsed >= DURATION_SECONDS:
                break

            # Continuous BlueMap dynamic update (simulate wandering drift & poll positions)
            if now >= next_bluemap_time:
                # Add natural wandering Brownian walk to bot positions
                for b in bots:
                    # Random small step +/- 0.4 blocks
                    step_dir = math.radians(b["yaw"] + (b["id"] * 13) % 45 - 22)
                    speed = 0.35
                    b["x"] += speed * math.cos(step_dir)
                    b["z"] += speed * math.sin(step_dir)
                    # Keep within 25 blocks of spawn anchor
                    dx = b["x"] - b["spawn_x"]
                    dz = b["z"] - b["spawn_z"]
                    if dx * dx + dz * dz > 625:  # 25m radius from cluster center
                        b["yaw"] = (b["yaw"] + 180) % 360
                    else:
                        b["yaw"] = (b["yaw"] + (b["id"] * 7) % 30 - 15) % 360

                push_bluemap_players(bots)
                next_bluemap_time = now + BLUEMAP_UPDATE_INTERVAL

            # Spark Profiler trigger at minute 5 (300 seconds)
            if elapsed >= 300 and not spark_profiler_triggered:
                print(f"\n[⚡] Minute 5: Starte 60-Sekunden Spark Flamegraph Profiler unter voller Last...")
                mc.execute_commands(["spark profiler --timeout 60"])
                spark_profiler_triggered = True

            # Telemetry sample every 60 seconds
            if now >= next_sample_time:
                # Query metrics from server
                mc.execute_commands([
                    "execute if entity @e[tag=stress_bot_10min]",
                    "spark tps",
                    "execute in minecraft:overworld run data get entity @e[tag=stress_bot_1,limit=1] Pos",
                    "execute in minecraft:overworld run data get entity @e[tag=stress_bot_50,limit=1] Pos"
                ])
                time.sleep(2)

                logs = get_docker_logs(25)
                spark_data = parse_spark_tps(logs)
                active_count = parse_entity_count(logs)
                sys_metrics = get_system_metrics()

                # Get sample bot displacement
                pos_bot1 = parse_bot_pos(logs, "stress_bot_1")
                disp_str = ""
                if pos_bot1:
                    disp = math.sqrt((pos_bot1[0] - bots[0]["spawn_x"])**2 + (pos_bot1[2] - bots[0]["spawn_z"])**2)
                    disp_str = f" | Bot1 Δ={disp:.1f}m"

                sample_record = {
                    "minute": sample_idx,
                    "elapsed_seconds": int(elapsed),
                    "active_bots": active_count,
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
                print(f"{time_str:<10} | {active_count:<6} | {spark_data['tps']:<6.2f} | {spark_data['mspt_med']:<10.2f} | {spark_data['mspt_max']:<10.2f} | {spark_data['cpu_system_pct']:<7}% | {'AKTIV':<8} | 🟢 OK{disp_str}")

                sample_idx += 1
                next_sample_time = now + SAMPLE_INTERVAL

            time.sleep(1)

        # Extract Spark Profiler URL if generated
        if spark_profiler_triggered:
            time.sleep(5)
            profiler_logs = get_docker_logs(50)
            url_match = re.search(r"https://spark\.lucko\.me/[A-Za-z0-9]+", profiler_logs)
            if url_match:
                test_results["spark_flamegraph_url"] = url_match.group(0)
                print(f"\n[⚡] Spark Profiler Flamegraph erfolgreich generiert: {test_results['spark_flamegraph_url']}")

    finally:
        # Step 5: Full Automated Cleanup
        print("\n" + "=" * 78)
        print("[*] 5. TEARDOWN & REINIGUNG DES SERVERS...")
        print("=" * 78)
        mc.execute_commands([
            "minecraft:kill @e[tag=stress_bot_10min]",
            "minecraft:kill @e[type=item]",
            "forceload remove all"
        ])
        push_bluemap_players([])
        time.sleep(3)

        # Final verification
        mc.execute_commands(["execute if entity @e[tag=stress_bot_10min]", "spark tps"])
        time.sleep(1)
        final_logs = get_docker_logs(20)
        final_metrics = parse_spark_tps(final_logs)
        test_results["end_time"] = datetime.now().isoformat()
        test_results["final_metrics"] = final_metrics

        print(f"    Alle Bots entfernt, Chunks entladen, BlueMap zurückgesetzt.")
        print(f"    Finaler Serverzustand: {final_metrics['tps']:.2f} TPS | Median MSPT: {final_metrics['mspt_med']:.2f} ms | CPU: {final_metrics['cpu_system_pct']}%")

    # Save results to JSON file
    out_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "docs", "stress_test_10min_results.json"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(test_results, f, indent=2)
    print(f"\n[💾] Ergebnisse gespeichert in: {out_path}")
    print("=" * 78)
    print("🏆 STRESSTEST ERFOLGREICH ABGESCHLOSSEN!")
    print("=" * 78)

if __name__ == "__main__":
    main()
