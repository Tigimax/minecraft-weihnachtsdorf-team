"""
Minecraft server admin and mod synchronization daemon / CLI.
Interacts with Pterodactyl Wings API on Node 2 to manage whitelist and LuckPerms roles.
"""

import logging
import sys
from typing import Any, Dict, List, Optional
import requests

from wd_identity.config import settings
from wd_identity.ldap_client import LDAPClient

logger = logging.getLogger(__name__)


class MinecraftSyncClient:
    def __init__(self):
        self.api_url = settings.WINGS_API_URL
        self.server_uuid = settings.WINGS_SERVER_UUID
        self.bearer_token = settings.WINGS_BEARER_TOKEN
        self.ldap = LDAPClient()

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

    def whitelist_add(self, ign: str) -> bool:
        """Adds a player to the Minecraft server whitelist."""
        return self.execute_commands([f"whitelist add {ign}"])

    def whitelist_remove(self, ign: str) -> bool:
        """Removes a player from the Minecraft server whitelist."""
        return self.execute_commands([f"whitelist remove {ign}"])

    def set_player_role(self, ign: str, role: str, mc_uuid: Optional[str] = None) -> bool:
        """Sets LuckPerms parent group and ensures player is whitelisted."""
        clean_role = role.lower().strip()
        if clean_role not in ("admin", "mod"):
            logger.warning(f"Unknown role {clean_role}, defaulting to mod")
            clean_role = "mod"

        cmds = [
            f"whitelist add {ign}",
            f"lp user {ign} parent set {clean_role}",
        ]
        if mc_uuid:
            cmds.append(f"lp user {mc_uuid} parent set {clean_role}")
        return self.execute_commands(cmds)

    def revoke_player_privileges(self, ign: str, mc_uuid: Optional[str] = None) -> bool:
        """Revokes server access and administrative permissions."""
        cmds = [
            f"whitelist remove {ign}",
            f"lp user {ign} parent remove admin",
            f"lp user {ign} parent remove mod",
            f"lp user {ign} parent set default",
        ]
        if mc_uuid:
            cmds.append(f"lp user {mc_uuid} parent remove admin")
            cmds.append(f"lp user {mc_uuid} parent remove mod")
            cmds.append(f"lp user {mc_uuid} parent set default")
        return self.execute_commands(cmds)

    def sync_all(self) -> Dict[str, Any]:
        """
        Polls LDAP admin and mod groups and synchronizes with Minecraft.
        Returns a detailed execution report.
        """
        admins, mods = self.ldap.get_admins_and_mods()
        commands_to_run = []
        synced_admins = []
        synced_mods = []
        skipped_users = []

        for admin in admins:
            ign = admin.get("minecraftName")
            mc_uuid = admin.get("minecraftUuid")
            if ign:
                commands_to_run.append(f"whitelist add {ign}")
                commands_to_run.append(f"lp user {ign} parent set admin")
                if mc_uuid:
                    commands_to_run.append(f"lp user {mc_uuid} parent set admin")
                synced_admins.append({"uid": admin["uid"], "ign": ign, "uuid": mc_uuid})
            else:
                skipped_users.append({"uid": admin["uid"], "reason": "No Minecraft IGN"})

        for mod in mods:
            ign = mod.get("minecraftName")
            mc_uuid = mod.get("minecraftUuid")
            if ign:
                commands_to_run.append(f"whitelist add {ign}")
                commands_to_run.append(f"lp user {ign} parent set mod")
                if mc_uuid:
                    commands_to_run.append(f"lp user {mc_uuid} parent set mod")
                synced_mods.append({"uid": mod["uid"], "ign": ign, "uuid": mc_uuid})
            else:
                skipped_users.append({"uid": mod["uid"], "reason": "No Minecraft IGN"})

        success = True
        if commands_to_run:
            success = self.execute_commands(commands_to_run)

        return {
            "success": success,
            "commands_executed": len(commands_to_run),
            "synced_admins": synced_admins,
            "synced_mods": synced_mods,
            "skipped_users": skipped_users,
        }


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    client = MinecraftSyncClient()
    logger.info("Starting Minecraft admin & mod privilege synchronization...")
    report = client.sync_all()
    if report["success"]:
        logger.info(
            f"Minecraft sync completed successfully: {len(report['synced_admins'])} admins, "
            f"{len(report['synced_mods'])} mods synced ({report['commands_executed']} commands sent)."
        )
        for a in report["synced_admins"]:
            logger.info(f"  [Admin] {a['uid']} -> IGN: {a['ign']}")
        for m in report["synced_mods"]:
            logger.info(f"  [Mod]   {m['uid']} -> IGN: {m['ign']}")
        for s in report["skipped_users"]:
            logger.warning(f"  [Skip]  {s['uid']} ({s['reason']})")
        sys.exit(0)
    else:
        logger.error("Minecraft privilege sync encountered errors.")
        sys.exit(1)


if __name__ == "__main__":
    main()
