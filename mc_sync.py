"""
Minecraft server admin and mod synchronization daemon / CLI.
Interacts with Pterodactyl Wings API on Node 2 to manage whitelist and LuckPerms roles.
Includes state tracking for idempotency and automatic revocation of removed staff.
"""

import json
import logging
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple
import requests

from wd_identity.config import settings
from wd_identity.ldap_client import LDAPClient
from wd_identity.utils import validate_minecraft_ign

logger = logging.getLogger(__name__)

# Preferred state file locations
STATE_PATHS = [
    Path("/var/lib/wd-identity/mc_sync_state.json"),
    Path("/opt/wd-identity/.mc_sync_state.json"),
    Path.home() / ".mc_sync_state.json",
    Path("/tmp/mc_sync_state.json"),
]

PROJECT_STATE_PATHS = [
    settings.PROJECT_STATE_FILE,
    Path("/var/lib/wd-identity/project_state.json"),
    Path("/opt/wd-identity/.project_state.json"),
    Path.home() / ".wd_project_state.json",
    Path("/tmp/wd_project_state.json"),
]


class MinecraftSyncClient:
    def __init__(self):
        self.api_url = settings.WINGS_API_URL
        self.server_uuid = settings.WINGS_SERVER_UUID
        self.bearer_token = settings.WINGS_BEARER_TOKEN
        self.ldap = LDAPClient()
        self.state_file = self._resolve_state_file()
        self.project_state_file = self._resolve_project_state_file()

    def _resolve_project_state_file(self) -> Path:
        for p in PROJECT_STATE_PATHS:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists() or p.stat().st_size == 0:
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump({"launched": False}, f, indent=2)
                return p
            except (OSError, PermissionError):
                continue
        fallback = Path("/tmp/wd_project_state.json")
        if not fallback.exists() or fallback.stat().st_size == 0:
            try:
                with open(fallback, "w", encoding="utf-8") as f:
                    json.dump({"launched": False}, f, indent=2)
            except Exception:
                pass
        return fallback

    def is_project_launched(self) -> bool:
        """Checks if the project has been launched (live whitelist for players)."""
        if self.project_state_file.exists() and self.project_state_file.stat().st_size > 0:
            try:
                with open(self.project_state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return bool(data.get("launched", False))
            except Exception as e:
                logger.warning(f"Could not read project state from {self.project_state_file}: {e}")
        return False

    def set_project_launched(self, launched: bool) -> bool:
        """Sets the project launched state flag."""
        try:
            with open(self.project_state_file, "w", encoding="utf-8") as f:
                json.dump({"launched": bool(launched)}, f, indent=2)
            return True
        except Exception as e:
            logger.error(f"Could not write project state to {self.project_state_file}: {e}")
            return False

    def _resolve_state_file(self) -> Path:
        for p in STATE_PATHS:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                # Test writeability
                if not p.exists():
                    p.touch()
                return p
            except (OSError, PermissionError):
                continue
        return Path("/tmp/mc_sync_state.json")

    def _load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not read state file {self.state_file}: {e}")
        return {}

    def _save_state(self, state: Dict[str, Any]) -> None:
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save state to {self.state_file}: {e}")

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def execute_commands(self, commands: List[str]) -> bool:
        """Sends console commands to the Minecraft server via Wings API."""
        if not commands:
            return True
        url = f"{self.api_url}/api/servers/{self.server_uuid}/commands"
        payload = {"commands": commands}
        try:
            res = requests.post(url, headers=self._headers, json=payload, timeout=10)
            if res.status_code in (200, 204):
                return True
            logger.error(
                f"Wings API returned status {res.status_code}: {res.text}"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to communicate with Wings API at {url}: {e}")
            return False

    def _build_set_role_commands(
        self, ign: str, role: str, mc_uuid: Optional[str] = None
    ) -> List[str]:
        if not validate_minecraft_ign(ign):
            logger.error(f"Invalid Minecraft IGN rejected: '{ign}'")
            return []
        cmds = [f"whitelist add {ign}"]
        target = mc_uuid if mc_uuid else ign
        cmds.append(f"lp user {target} parent set {role}")
        return cmds

    def _build_revoke_commands(
        self, ign: str, mc_uuid: Optional[str] = None
    ) -> List[str]:
        if not validate_minecraft_ign(ign):
            logger.error(f"Invalid Minecraft IGN rejected: '{ign}'")
            return []
        cmds = [
            f"whitelist remove {ign}",
        ]
        target = mc_uuid if mc_uuid else ign
        cmds.append(f"lp user {target} parent remove admin")
        cmds.append(f"lp user {target} parent remove mod")
        cmds.append(f"lp user {target} parent set default")
        return cmds

    def whitelist_add(self, ign: str) -> bool:
        """Adds a player to the Minecraft server whitelist."""
        if not validate_minecraft_ign(ign):
            logger.error(f"Invalid Minecraft IGN for whitelist: '{ign}'")
            return False
        return self.execute_commands([f"whitelist add {ign}"])

    def whitelist_remove(self, ign: str) -> bool:
        """Removes a player from the Minecraft server whitelist."""
        if not validate_minecraft_ign(ign):
            logger.error(f"Invalid Minecraft IGN for whitelist removal: '{ign}'")
            return False
        return self.execute_commands([f"whitelist remove {ign}"])

    def set_player_role(self, ign: str, role: str, mc_uuid: Optional[str] = None) -> bool:
        """Sets LuckPerms parent group and ensures player is whitelisted (if launched or staff)."""
        clean_role = role.lower().strip() if role else "player"
        if clean_role == "player":
            if self.is_project_launched():
                cmds = [f"whitelist add {ign}"]
                target = mc_uuid if mc_uuid else ign
                cmds.append(f"lp user {target} parent set default")
                return self.execute_commands(cmds)
            else:
                logger.info(
                    f"Player '{ign}' registered in pre-launch mode. Whitelist will be activated at project launch."
                )
                return True

        if clean_role not in ("admin", "mod"):
            logger.warning(f"Unknown role {clean_role}, defaulting to mod")
            clean_role = "mod"

        cmds = self._build_set_role_commands(ign, clean_role, mc_uuid)
        if not cmds:
            return False
        return self.execute_commands(cmds)

    def revoke_player_privileges(self, ign: str, mc_uuid: Optional[str] = None) -> bool:
        """Revokes server access and administrative permissions."""
        cmds = self._build_revoke_commands(ign, mc_uuid)
        if not cmds:
            return False
        return self.execute_commands(cmds)

    def ban_player(self, ign: str, reason: str = "Regelverstoß") -> bool:
        """Bans a player on Minecraft server and removes them from whitelist."""
        if not validate_minecraft_ign(ign):
            logger.error(f"Invalid Minecraft IGN for ban: '{ign}'")
            return False
        clean_reason = reason.replace("\n", " ").replace('"', "'")
        cmds = [
            f'ban {ign} {clean_reason}',
            f'whitelist remove {ign}',
        ]
        return self.execute_commands(cmds)

    def pardon_player(self, ign: str) -> bool:
        """Unbans player on Minecraft server and re-adds to whitelist if project is launched."""
        if not validate_minecraft_ign(ign):
            logger.error(f"Invalid Minecraft IGN for pardon: '{ign}'")
            return False
        cmds = [f'pardon {ign}']
        if self.is_project_launched():
            cmds.append(f'whitelist add {ign}')
        return self.execute_commands(cmds)

    def launch_project(self) -> Dict[str, Any]:
        """Sets launch state to True and immediately whitelists all registered players."""
        self.set_project_launched(True)
        return self.sync_all(force=True)

    def lock_project(self) -> Dict[str, Any]:
        """Sets launch state to False and removes normal players from whitelist (staff retained)."""
        self.set_project_launched(False)
        return self.sync_all(force=True)

    def sync_all(self, force: bool = False) -> Dict[str, Any]:
        """
        Polls LDAP admin, mod, and player groups and synchronizes with Minecraft.
        Admins and mods are always kept whitelisted.
        Regular players are only whitelisted if project is launched (is_project_launched() is True).
        """
        admins, mods = self.ldap.get_admins_and_mods()
        players = self.ldap.get_players()
        is_launched = self.is_project_launched()
        prev_state = self._load_state()
        current_state: Dict[str, Any] = {}

        commands_to_run: List[str] = []
        synced_admins: List[Dict[str, Any]] = []
        synced_mods: List[Dict[str, Any]] = []
        synced_players: List[Dict[str, Any]] = []
        staged_players: List[Dict[str, Any]] = []
        unchanged_users: List[Dict[str, Any]] = []
        revoked_users: List[Dict[str, Any]] = []
        skipped_users: List[Dict[str, Any]] = []

        # Process Admins
        for admin in admins:
            uid = admin["uid"]
            ign = admin.get("minecraftName")
            mc_uuid = admin.get("minecraftUuid")
            if not ign:
                skipped_users.append({"uid": uid, "reason": "No Minecraft IGN"})
                continue
            if not validate_minecraft_ign(ign):
                skipped_users.append({"uid": uid, "reason": f"Invalid Minecraft IGN: {ign}"})
                continue

            current_state[uid] = {"role": "admin", "ign": ign, "uuid": mc_uuid, "whitelisted": True}
            if force or uid not in prev_state or prev_state[uid] != current_state[uid]:
                commands_to_run.extend(self._build_set_role_commands(ign, "admin", mc_uuid))
                synced_admins.append({"uid": uid, "ign": ign, "uuid": mc_uuid})
            else:
                unchanged_users.append({"uid": uid, "ign": ign, "role": "admin"})

        # Process Mods
        for mod in mods:
            uid = mod["uid"]
            ign = mod.get("minecraftName")
            mc_uuid = mod.get("minecraftUuid")
            if not ign:
                skipped_users.append({"uid": uid, "reason": "No Minecraft IGN"})
                continue
            if not validate_minecraft_ign(ign):
                skipped_users.append({"uid": uid, "reason": f"Invalid Minecraft IGN: {ign}"})
                continue

            current_state[uid] = {"role": "mod", "ign": ign, "uuid": mc_uuid, "whitelisted": True}
            if force or uid not in prev_state or prev_state[uid] != current_state[uid]:
                commands_to_run.extend(self._build_set_role_commands(ign, "mod", mc_uuid))
                synced_mods.append({"uid": uid, "ign": ign, "uuid": mc_uuid})
            else:
                unchanged_users.append({"uid": uid, "ign": ign, "role": "mod"})

        # Process Players
        for player in players:
            uid = player["uid"]
            ign = player.get("minecraftName")
            mc_uuid = player.get("minecraftUuid")
            if not ign:
                skipped_users.append({"uid": uid, "reason": "No Minecraft IGN"})
                continue
            if not validate_minecraft_ign(ign):
                skipped_users.append({"uid": uid, "reason": f"Invalid Minecraft IGN: {ign}"})
                continue

            target = mc_uuid if mc_uuid else ign
            if is_launched:
                current_state[uid] = {"role": "player", "ign": ign, "uuid": mc_uuid, "whitelisted": True}
                if force or uid not in prev_state or prev_state[uid] != current_state[uid]:
                    commands_to_run.append(f"whitelist add {ign}")
                    commands_to_run.append(f"lp user {target} parent set default")
                    synced_players.append({"uid": uid, "ign": ign, "uuid": mc_uuid})
                else:
                    unchanged_users.append({"uid": uid, "ign": ign, "role": "player"})
            else:
                # Pre-Launch mode: players must NOT be whitelisted on server
                current_state[uid] = {"role": "player", "ign": ign, "uuid": mc_uuid, "whitelisted": False}
                if prev_state.get(uid, {}).get("whitelisted") is True:
                    commands_to_run.append(f"whitelist remove {ign}")
                    revoked_users.append({"uid": uid, "ign": ign, "uuid": mc_uuid})
                else:
                    staged_players.append({"uid": uid, "ign": ign, "uuid": mc_uuid})

        # Check for removed staff or players (in prev_state but not in current_state)
        for old_uid, old_data in prev_state.items():
            if old_uid not in current_state:
                old_ign = old_data.get("ign")
                old_uuid = old_data.get("uuid")
                old_whitelisted = old_data.get("whitelisted", True)
                if old_ign and validate_minecraft_ign(old_ign) and old_whitelisted:
                    logger.info(f"Revoking Minecraft access for former member {old_uid} ({old_ign})")
                    commands_to_run.extend(self._build_revoke_commands(old_ign, old_uuid))
                    revoked_users.append({"uid": old_uid, "ign": old_ign, "uuid": old_uuid})

        success = True
        if commands_to_run:
            success = self.execute_commands(commands_to_run)
            if success:
                self._save_state(current_state)
        else:
            self._save_state(current_state)

        return {
            "success": success,
            "is_launched": is_launched,
            "commands_executed": len(commands_to_run),
            "synced_admins": synced_admins,
            "synced_mods": synced_mods,
            "synced_players": synced_players,
            "staged_players": staged_players,
            "unchanged_users": unchanged_users,
            "revoked_users": revoked_users,
            "skipped_users": skipped_users,
        }

    def get_server_telemetry(self) -> Dict[str, Any]:
        """Fetches utilization metrics from Pterodactyl Wings API."""
        url = f"{self.api_url}/api/servers/{self.server_uuid}"
        try:
            r = requests.get(url, headers=self._headers, timeout=5)
            if not r.ok:
                return {}
            data = r.json()
            util = data.get("utilization", {})
            mem_bytes = util.get("memory_bytes", 0)
            mem_limit = util.get("memory_limit_bytes", 0)
            return {
                "state": util.get("state") or data.get("state", "unknown"),
                "cpu_percent": round(util.get("cpu_absolute", 0.0), 1),
                "memory_used_mb": round(mem_bytes / (1024 * 1024), 1),
                "memory_max_mb": round(mem_limit / (1024 * 1024), 1),
                "memory_percent": round((mem_bytes / mem_limit) * 100, 1) if mem_limit else 0.0,
                "disk_used_mb": round(util.get("disk_bytes", 0) / (1024 * 1024), 1),
                "uptime_seconds": round(util.get("uptime", 0) / 1000),
            }
        except Exception as e:
            logger.error(f"Failed to fetch server telemetry: {e}")
            return {}

    def get_live_tps(self) -> str:
        """Fetches current Purpur TPS via console."""
        self.execute_commands(["tps"])
        import time
        time.sleep(0.5)
        url = f"{self.api_url}/api/servers/{self.server_uuid}/logs"
        try:
            r = requests.get(url, headers=self._headers, timeout=5)
            if not r.ok:
                return "20.0 (Normal)"
            logs = r.json().get("data", [])
            for line in reversed(logs):
                if "TPS from last" in line:
                    return line.split("TPS from last")[-1].split(":", 1)[-1].strip()
            return "20.0, 20.0, 20.0 (Normal)"
        except Exception:
            return "Unbekannt"

    def get_online_players(self) -> Tuple[int, int, List[str]]:
        """Queries currently connected players."""
        self.execute_commands(["list"])
        import time, re
        time.sleep(0.5)
        url = f"{self.api_url}/api/servers/{self.server_uuid}/logs"
        try:
            r = requests.get(url, headers=self._headers, timeout=5)
            if not r.ok:
                return (0, 120, [])
            logs = r.json().get("data", [])
            for line in reversed(logs):
                if "players online" in line:
                    m = re.search(r"There are (\d+) (?:out of maximum|of a max of) (\d+) players online(?:\: (.*))?", line)
                    if m:
                        count = int(m.group(1))
                        max_p = int(m.group(2))
                        names_raw = m.group(3) or ""
                        names = [n.strip() for n in names_raw.split(",") if n.strip()]
                        return (count, max_p, names)
            return (0, 120, [])
        except Exception:
            return (0, 120, [])

    def broadcast_ingame(self, message: str, title: Optional[str] = None, subtitle: Optional[str] = None) -> bool:
        """Sends chat notification and optional title to all online players."""
        cmds = []
        clean_msg = message.replace('"', '\\"')
        cmds.append(f'tellraw @a {{"text":"[Weihnachtsdorf] {clean_msg}","color":"gold"}}')
        if title:
            clean_title = title.replace('"', '\\"')
            cmds.append(f'title @a title {{"text":"{clean_title}","color":"red","bold":true}}')
        if subtitle:
            clean_sub = subtitle.replace('"', '\\"')
            cmds.append(f'title @a subtitle {{"text":"{clean_sub}","color":"yellow"}}')
        return self.execute_commands(cmds)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    import argparse
    parser = argparse.ArgumentParser(description="Weihnachtsdorf 7 Minecraft Admin Privilege Sync")
    parser.add_argument("--force", "-f", action="store_true", help="Force command execution even if state unchanged")
    args = parser.parse_args()

    client = MinecraftSyncClient()
    logger.info("Starting Minecraft admin & mod privilege synchronization...")
    report = client.sync_all(force=args.force)
    if report["success"]:
        if report["commands_executed"] > 0:
            logger.info(
                f"Minecraft sync completed: {len(report['synced_admins'])} admins, "
                f"{len(report['synced_mods'])} mods updated, {len(report['revoked_users'])} revoked "
                f"({report['commands_executed']} commands sent)."
            )
            for a in report["synced_admins"]:
                logger.info(f"  [Admin Updated] {a['uid']} -> IGN: {a['ign']}")
            for m in report["synced_mods"]:
                logger.info(f"  [Mod Updated]   {m['uid']} -> IGN: {m['ign']}")
            for r in report["revoked_users"]:
                logger.info(f"  [Revoked]       {r['uid']} -> IGN: {r['ign']}")
        else:
            logger.info(
                f"Minecraft state unchanged: {len(report['unchanged_users'])} staff members in sync. "
                f"No commands sent."
            )
        for s in report["skipped_users"]:
            logger.warning(f"  [Skip]  {s['uid']} ({s['reason']})")
        sys.exit(0)
    else:
        logger.error("Minecraft privilege sync encountered errors.")
        sys.exit(1)


if __name__ == "__main__":
    main()
