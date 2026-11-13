"""
Minecraft server admin and mod synchronization daemon / CLI.
Interacts with Pterodactyl Wings API on Node 2 to manage whitelist and LuckPerms roles.
Includes state tracking for idempotency and automatic revocation of removed staff.
"""

from datetime import datetime, timezone, timedelta
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import requests

try:
    import fcntl
except ImportError:
    fcntl = None

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

MOD_GRANTS_PATHS = [
    Path("/var/lib/wd-identity/mod_build_grants.json"),
    Path("/opt/wd-identity/.mod_build_grants.json"),
    Path.home() / ".mod_build_grants.json",
    Path("/tmp/mod_build_grants.json"),
]


def new_log_lines(prev: List[str], cur: List[str]) -> List[str]:
    """
    Extracts newly appended log lines between prev and cur polls.
    Finds the longest overlap k (suffix of prev == prefix of cur) and returns cur[k:].
    If prev is empty, returns [] (suppresses back-posting on initial poll).
    If k == 0 and prev is not empty, returns cur completely (log rotation / complete switch).
    """
    if not prev or not cur:
        return []

    max_k = min(len(prev), len(cur))
    for k in range(max_k, 0, -1):
        if prev[-k:] == cur[:k]:
            return cur[k:]

    return list(cur)


def _atomic_write_json(file_path: Path, data: Any, use_lock: bool = True) -> bool:
    """
    Writes JSON data to file_path atomically using a temporary file in the same directory,
    fsync, and atomic rename (os.replace). Uses flock if available to prevent concurrent write collisions.
    """
    tmp_path = None
    lock_fd = None
    try:
        parent_dir = file_path.parent
        parent_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = parent_dir / f".{file_path.name}.tmp.{os.getpid()}_{time.time_ns()}"
        lock_path = parent_dir / f".{file_path.name}.lock"

        if use_lock and fcntl:
            try:
                lock_fd = open(lock_path, "w")
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            except Exception as le:
                logger.debug(f"Could not acquire flock on {lock_path}: {le}")
                lock_fd = None

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, file_path)
            return True
        finally:
            if lock_fd:
                try:
                    fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    pass
                try:
                    if lock_path.exists():
                        lock_path.unlink()
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Could not save JSON atomically to {file_path}: {e}")
        return False
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


class MinecraftSyncClient:
    def __init__(self):
        self.api_url = settings.WINGS_API_URL
        self.server_uuid = settings.WINGS_SERVER_UUID
        self.bearer_token = settings.WINGS_BEARER_TOKEN
        self.ldap = LDAPClient()
        self.state_file = self._resolve_state_file()
        self.project_state_file = self._resolve_project_state_file()
        self.mod_grants_file = self._resolve_mod_grants_file()

    def _resolve_mod_grants_file(self) -> Path:
        for p in MOD_GRANTS_PATHS:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists() or p.stat().st_size == 0:
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump({}, f, indent=2)
                return p
            except (OSError, PermissionError):
                continue
        fallback = Path("/tmp/mod_build_grants.json")
        if not fallback.exists() or fallback.stat().st_size == 0:
            try:
                with open(fallback, "w", encoding="utf-8") as f:
                    json.dump({}, f, indent=2)
            except Exception:
                pass
        return fallback

    def _load_mod_grants(self) -> Dict[str, Any]:
        if self.mod_grants_file.exists():
            try:
                with open(self.mod_grants_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not read mod grants file {self.mod_grants_file}: {e}")
        return {}

    def _save_mod_grants(self, grants: Dict[str, Any]) -> None:
        _atomic_write_json(self.mod_grants_file, grants, use_lock=True)

    def get_active_mod_grants(self) -> Dict[str, Any]:
        """Returns all currently active (unexpired) temporary mod build grants."""
        raw = self._load_mod_grants()
        now = datetime.now(timezone.utc)
        active = {}
        has_expired = False

        for k, v in raw.items():
            exp_str = v.get("expires_at")
            if not exp_str:
                continue
            try:
                exp_dt = datetime.fromisoformat(exp_str)
                if exp_dt > now:
                    active[k] = v
                else:
                    has_expired = True
            except Exception:
                has_expired = True

        if has_expired:
            self._save_mod_grants(active)

        return active

    def has_active_mod_grant(self, uid: str, ign: Optional[str] = None) -> bool:
        """Checks if a given mod UID or IGN currently has an active build grant."""
        active = self.get_active_mod_grants()
        if uid in active:
            return True
        if ign:
            for g in active.values():
                if g.get("ign") and g.get("ign").lower() == ign.lower():
                    return True
        return False

    def setup_mod_permissions(self) -> bool:
        """Configures LuckPerms group 'mod' with required moderator permissions."""
        cmds = [
            'lp group mod meta setprefix 50 "&9[Mod] &r"',
            # WorldEdit
            "lp group mod permission set worldedit.* true",
            # CoreProtect
            "lp group mod permission set coreprotect.* true",
            # Teleport & Info
            "lp group mod permission set minecraft.command.tp true",
            "lp group mod permission set minecraft.command.teleport true",
            "lp group mod permission set essentials.tp true",
            "lp group mod permission set essentials.tphere true",
            "lp group mod permission set essentials.tpa true",
            "lp group mod permission set essentials.tpahere true",
            "lp group mod permission set essentials.invsee true",
            "lp group mod permission set essentials.vanish true",
            "lp group mod permission set essentials.gamemode true",
            "lp group mod permission set essentials.fly true",
            # Moderation
            "lp group mod permission set minecraft.command.kick true",
            "lp group mod permission set minecraft.command.ban true",
            "lp group mod permission set essentials.kick true",
            "lp group mod permission set essentials.ban true",
            "lp group mod permission set essentials.tempban true",
            "lp group mod permission set essentials.mute true",
            "lp group mod permission set essentials.tempmute true",
            "lp group mod permission set essentials.warn true",
            "lp group mod permission set essentials.helpop.receive true",
        ]
        return self.execute_commands(cmds)

    def setup_admin_permissions(self) -> bool:
        """Configures LuckPerms group 'admin' with required admin prefix and permissions."""
        cmds = [
            'lp group admin meta setprefix 100 "&c[Admin] &r"',
            "lp group admin permission set * true",
        ]
        return self.execute_commands(cmds)

    def grant_mod_build_access(
        self, target: str, duration_hours: float, initiator: str = "Admin"
    ) -> Dict[str, Any]:
        """
        Grants a moderator temporary building rights (whitelist + WorldEdit + mod role)
        during the pre-launch phase for the specified number of hours.
        """
        clean_target = str(target).strip().lstrip("@")
        user = (
            self.ldap.get_user(clean_target)
            or self.ldap.search_by_discord(clean_target)
            or self.ldap.search_by_minecraft(clean_target)
        )
        if not user:
            raise ValueError(f"Benutzer '{clean_target}' konnte in LDAP nicht gefunden werden.")

        uid = user["uid"]
        user_dn = user["dn"]
        mod_dns = set(self.ldap.get_group_members(self.ldap.mod_group_dn))
        if user_dn not in mod_dns:
            raise ValueError(f"Benutzer '{uid}' ist kein Mitglied der Gruppe 'mod'. Baurechte können nur Moderatoren gewährt werden.")

        ign = user.get("minecraftName")
        if not ign:
            raise ValueError(f"Moderator '{uid}' hat keinen Minecraft Ingame-Namen verknüpft.")
        if not validate_minecraft_ign(ign):
            raise ValueError(f"Ungültiger Minecraft Ingame-Name '{ign}' für Moderator '{uid}'.")

        mc_uuid = user.get("minecraftUuid")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=duration_hours)

        grants = self._load_mod_grants()
        grants[uid] = {
            "uid": uid,
            "ign": ign,
            "uuid": mc_uuid,
            "granted_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "duration_hours": duration_hours,
            "initiator": initiator,
        }
        self._save_mod_grants(grants)

        # Ensure permissions and whitelist
        self.setup_mod_permissions()
        target_lp = mc_uuid if (mc_uuid and re.match(r"^[0-9a-fA-F-]{36}$", str(mc_uuid))) else ign
        self.execute_commands([
            f"whitelist add {ign}",
            f"lp user {target_lp} parent set mod",
        ])

        # Send DM notification to mod
        discord_handle = user.get("discordId") or user.get("discordName")
        display_handle = user.get("discordName") or uid
        if discord_handle:
            try:
                from wd_identity.utils import send_dm
                exp_local = expires_at.strftime("%H:%M UTC")
                msg = (
                    f"🔨 **Temporäre Baurechte aktiviert!**\n\n"
                    f"Hallo {display_handle}, dir wurden soeben von **{initiator}** für **{duration_hours:g} Stunde(n)** "
                    f"Baurechte auf dem Server freigeschaltet (gültig bis {exp_local}).\n"
                    f"Du kannst den Server ab sofort betreten und WorldEdit für Vorbereitungen nutzen.\n"
                    f"Nach Ablauf des Zeitfensters wird dein Zugang automatisch wieder in den Wartungsmodus versetzt."
                )
                send_dm(discord_handle, msg)
            except Exception as e:
                logger.warning(f"Could not send build grant DM to {uid}: {e}")

        # Audit logging
        try:
            from wd_identity.audit_logger import log_admin_event, COLOR_INFO
            log_admin_event(
                title="🔨 Temporäre Baurechte für Mod erteilt",
                description=f"Dem Moderator **{uid}** (`{ign}`) wurden für **{duration_hours:g}h** Baurechte auf dem Server erteilt.",
                color=COLOR_INFO,
                fields=[
                    {"name": "Moderator", "value": f"`{uid}` ({ign})", "inline": True},
                    {"name": "Dauer", "value": f"{duration_hours:g} Stunde(n)", "inline": True},
                    {"name": "Gültig bis", "value": expires_at.strftime("%Y-%m-%d %H:%M UTC"), "inline": True},
                    {"name": "Erteilt von", "value": initiator, "inline": True},
                ],
                author_name=initiator,
            )
        except Exception as e:
            logger.warning(f"Failed to post audit log for mod build grant: {e}")

        return grants[uid]

    def revoke_mod_build_access(self, target: str, initiator: str = "Admin") -> Dict[str, Any]:
        """Revokes temporary building access from a moderator."""
        clean_target = str(target).strip().lstrip("@")
        user = (
            self.ldap.get_user(clean_target)
            or self.ldap.search_by_discord(clean_target)
            or self.ldap.search_by_minecraft(clean_target)
        )
        uid = user["uid"] if user else clean_target
        ign = user.get("minecraftName") if user else clean_target

        grants = self._load_mod_grants()
        had_grant = grants.pop(uid, None)
        self._save_mod_grants(grants)

        if not self.is_project_launched() and ign:
            if validate_minecraft_ign(ign):
                clean_kick_msg = "Temporäre Baurechte beendet (Pre-Launch)".replace("\r", " ").replace("\n", " ").replace('"', '')
                self.execute_commands([
                    f"whitelist remove {ign}",
                    f'kick {ign} "{clean_kick_msg}"',
                ])
            else:
                logger.warning(f"Skipping in-game commands for invalid Minecraft IGN: {ign}")

        try:
            from wd_identity.audit_logger import log_admin_event, COLOR_WARNING
            log_admin_event(
                title="🛑 Temporäre Baurechte für Mod beendet",
                description=f"Die Baurechte von **{uid}** wurden vorzeitig beendet.",
                color=COLOR_WARNING,
                fields=[
                    {"name": "Moderator", "value": f"`{uid}`", "inline": True},
                    {"name": "Beendet von", "value": initiator, "inline": True},
                ],
                author_name=initiator,
            )
        except Exception as e:
            logger.warning(f"Failed to post audit log for mod build revocation: {e}")

        return {"success": True, "uid": uid, "had_grant": bool(had_grant)}

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
        return _atomic_write_json(
            self.project_state_file, {"launched": bool(launched)}, use_lock=True
        )

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
        _atomic_write_json(self.state_file, state, use_lock=True)

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

        if clean_role == "mod":
            self.setup_mod_permissions()
            has_grant = self.has_active_mod_grant(uid=ign, ign=ign)
            if self.is_project_launched() or has_grant:
                cmds = self._build_set_role_commands(ign, "mod", mc_uuid)
                return self.execute_commands(cmds)
            else:
                logger.info(
                    f"Mod '{ign}' registered in pre-launch mode without active build grant. Whitelist will activate at launch or upon grant."
                )
                return True

        if clean_role != "admin":
            logger.warning(f"Unknown role {clean_role}, defaulting to admin")

        self.setup_admin_permissions()
        cmds = self._build_set_role_commands(ign, "admin", mc_uuid)
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
        """Sets launch state to True, unlocks BlueMap in Authentik, and whitelists all registered players."""
        self.set_project_launched(True)
        try:
            from wd_identity.authentik_client import AuthentikClient
            authentik = AuthentikClient()
            authentik.set_project_launched(True)
        except Exception as e:
            logger.warning(f"Could not synchronize launch state with Authentik: {e}")
        return self.sync_all(force=True)

    def lock_project(self) -> Dict[str, Any]:
        """Sets launch state to False, locks BlueMap in Authentik (pre-launch), and removes normal players from whitelist."""
        self.set_project_launched(False)
        try:
            from wd_identity.authentik_client import AuthentikClient
            authentik = AuthentikClient()
            authentik.set_project_launched(False)
        except Exception as e:
            logger.warning(f"Could not synchronize lock state with Authentik: {e}")
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
        active_grants = self.get_active_mod_grants()
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

            has_grant = uid in active_grants or any(g.get("ign", "").lower() == ign.lower() for g in active_grants.values())
            is_mod_whitelisted = is_launched or has_grant

            if is_mod_whitelisted:
                current_state[uid] = {"role": "mod", "ign": ign, "uuid": mc_uuid, "whitelisted": True}
                if force or uid not in prev_state or prev_state[uid] != current_state[uid]:
                    commands_to_run.extend(self._build_set_role_commands(ign, "mod", mc_uuid))
                    synced_mods.append({"uid": uid, "ign": ign, "uuid": mc_uuid})
                else:
                    unchanged_users.append({"uid": uid, "ign": ign, "role": "mod"})
            else:
                current_state[uid] = {"role": "mod", "ign": ign, "uuid": mc_uuid, "whitelisted": False}
                if prev_state.get(uid, {}).get("whitelisted") is True:
                    commands_to_run.append(f"whitelist remove {ign}")
                    commands_to_run.append(f'kick {ign} "Bau-Zeitfenster abgelaufen (Pre-Launch)"')
                    revoked_users.append({"uid": uid, "ign": ign, "uuid": mc_uuid})
                else:
                    staged_players.append({"uid": uid, "ign": ign, "uuid": mc_uuid, "role": "mod"})

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
            config_build = data.get("configuration", {}).get("build", {})
            mem_bytes = util.get("memory_bytes", 0)
            mem_limit = util.get("memory_limit_bytes", 0)
            disk_bytes = util.get("disk_bytes", 0)
            disk_limit_mb = config_build.get("disk_space", 0)
            return {
                "state": util.get("state") or data.get("state", "unknown"),
                "cpu_percent": round(util.get("cpu_absolute", 0.0), 1),
                "memory_used_mb": round(mem_bytes / (1024 * 1024), 1),
                "memory_max_mb": round(mem_limit / (1024 * 1024), 1),
                "memory_percent": round((mem_bytes / mem_limit) * 100, 1) if mem_limit else 0.0,
                "disk_used_mb": round(disk_bytes / (1024 * 1024), 1),
                "disk_max_mb": round(disk_limit_mb, 1),
                "disk_percent": round((disk_bytes / (disk_limit_mb * 1024 * 1024)) * 100, 1) if disk_limit_mb else 0.0,
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

    def get_log_lines(self, size: int = 100) -> List[str]:
        """Fetches the latest console log lines from Wings API."""
        url = f"{self.api_url}/api/servers/{self.server_uuid}/logs?size={size}"
        try:
            r = requests.get(url, headers=self._headers, timeout=5)
            if not r.ok:
                return []
            data = r.json()
            if isinstance(data, dict):
                return data.get("data", [])
            return []
        except Exception:
            return []

    def run_and_capture(self, cmd: str, wait: float = 0.8) -> List[str]:
        """Executes a console command and captures new log output lines produced by it."""
        before = self.get_log_lines()
        ok = self.execute_commands([cmd])
        if not ok:
            raise RuntimeError(f"Failed to execute command '{cmd}' via Wings API")
        time.sleep(wait)
        after = self.get_log_lines()
        if not before:
            return list(after)
        return new_log_lines(before, after)

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

    def broadcast_ingame(
        self,
        message: Optional[str] = None,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
    ) -> bool:
        """Sends chat notification and optional title to all online players."""
        cmds = []
        clean_msg = (message or "").replace("\r", " ").replace("\n", " ").strip()
        if clean_msg:
            msg_payload = json.dumps({"text": f"[Weihnachtsdorf] {clean_msg}", "color": "gold"})
            cmds.append(f"execute in minecraft:overworld run tellraw @a {msg_payload}")
        if title:
            clean_title = title.replace("\r", " ").replace("\n", " ").strip()
            if clean_title:
                title_payload = json.dumps({"text": clean_title, "color": "red", "bold": True})
                cmds.append(f"title @a title {title_payload}")
        if subtitle:
            clean_sub = subtitle.replace("\r", " ").replace("\n", " ").strip()
            if clean_sub:
                sub_payload = json.dumps({"text": clean_sub, "color": "yellow"})
                cmds.append(f"title @a subtitle {sub_payload}")
        if not cmds:
            return True
        return self.execute_commands(cmds)

    def restart_server(self) -> bool:
        """Restarts the Minecraft server via Wings API power action."""
        try:
            url = f"{self.api_url}/api/servers/{self.server_uuid}/power"
            r = requests.post(url, headers=self._headers, json={"action": "restart"}, timeout=10)
            return r.status_code in (200, 202, 204)
        except Exception as e:
            logger.error(f"Failed to restart Minecraft server via Wings API: {e}")
            return False


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
