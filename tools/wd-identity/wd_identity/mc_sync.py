"""
Minecraft server admin and mod synchronization daemon / CLI.
Interacts with Pterodactyl Wings API on Node 2 to manage whitelist and LuckPerms roles.
Includes state tracking for idempotency and automatic revocation of removed staff.
"""

import json
import logging
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional
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


class MinecraftSyncClient:
    def __init__(self):
        self.api_url = settings.WINGS_API_URL
        self.server_uuid = settings.WINGS_SERVER_UUID
        self.bearer_token = settings.WINGS_BEARER_TOKEN
        self.ldap = LDAPClient()
        self.state_file = self._resolve_state_file()

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
        """Sets LuckPerms parent group and ensures player is whitelisted."""
        clean_role = role.lower().strip() if role else "mod"
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

    def sync_all(self, force: bool = False) -> Dict[str, Any]:
        """
        Polls LDAP admin and mod groups and synchronizes with Minecraft.
        Uses state diffing for idempotency and automatically revokes removed staff.
        """
        admins, mods = self.ldap.get_admins_and_mods()
        prev_state = self._load_state() if not force else {}
        current_state: Dict[str, Any] = {}

        commands_to_run: List[str] = []
        synced_admins: List[Dict[str, Any]] = []
        synced_mods: List[Dict[str, Any]] = []
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

            current_state[uid] = {"role": "admin", "ign": ign, "uuid": mc_uuid}
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

            current_state[uid] = {"role": "mod", "ign": ign, "uuid": mc_uuid}
            if force or uid not in prev_state or prev_state[uid] != current_state[uid]:
                commands_to_run.extend(self._build_set_role_commands(ign, "mod", mc_uuid))
                synced_mods.append({"uid": uid, "ign": ign, "uuid": mc_uuid})
            else:
                unchanged_users.append({"uid": uid, "ign": ign, "role": "mod"})

        # Check for removed staff
        for old_uid, old_data in prev_state.items():
            if old_uid not in current_state:
                old_ign = old_data.get("ign")
                old_uuid = old_data.get("uuid")
                if old_ign and validate_minecraft_ign(old_ign):
                    logger.info(f"Revoking Minecraft access for former staff member {old_uid} ({old_ign})")
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
            "commands_executed": len(commands_to_run),
            "synced_admins": synced_admins,
            "synced_mods": synced_mods,
            "unchanged_users": unchanged_users,
            "revoked_users": revoked_users,
            "skipped_users": skipped_users,
        }


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
