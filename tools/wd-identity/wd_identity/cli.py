"""
Command-line interface (wd-admin) for Weihnachtsdorf 7.
Commands:
- invite <discord_name> [--role admin|mod] [--ign <minecraft_ign>] [--username <uid>]
- reset <discord_name>
- list
- revoke <discord_name>
- sync [--force]
"""

import sys
import time
from typing import Optional, Tuple
import click

from wd_identity.audit_logger import COLOR_ERROR, COLOR_SUCCESS, COLOR_WARNING, log_admin_event
from wd_identity.authentik_client import AuthentikClient
from wd_identity import applications
from wd_identity.config import settings
from wd_identity.discord_bot import send_dm
from wd_identity.invitations import create_user_invitation
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient
from wd_identity.mojang_client import get_player_profile
from wd_identity.planka_sync import PlankaSyncClient
from wd_identity.utils import (
    check_is_discord_admin,
    sanitize_email,
    sanitize_username,
    validate_minecraft_ign,
)


@click.group()
def cli():
    """Weihnachtsdorf 7 Identity Administration Tool (wd-admin)"""
    pass


@cli.command("invite")
@click.argument("discord_name")
@click.option(
    "--role",
    "-r",
    type=click.Choice(["admin", "mod", "player"], case_sensitive=False),
    default="player",
    help="Role to assign (admin, mod, or player). Defaults to player.",
)
@click.option(
    "--ign",
    help="Minecraft Ingame Name (optional, can be provided during enrollment).",
)
@click.option(
    "--username",
    "-u",
    help="Explicit username/UID in LDAP (defaults to sanitized discord_name).",
)
@click.option(
    "--force-manual",
    is_flag=True,
    help="Erstellt den Account auch dann, wenn die Discord-DM nicht zugestellt werden kann (für manuelle Weitergabe).",
)
def invite_cmd(discord_name: str, role: str, ign: Optional[str], username: Optional[str], force_manual: bool):
    """
    Invites a new player, mod, or admin to Weihnachtsdorf 7.
    Creates LDAP entry, generates Authentik enrollment link, and sends Discord DM.
    """
    try:
        res = create_user_invitation(
            discord_name=discord_name,
            role=role,
            ign=ign,
            username=username,
            inviter="CLI (wd-admin)",
        )
    except PermissionError as e:
        click.secho(f"\n[-] {e}\n", fg="red", bold=True)
        sys.exit(1)
    except (ValueError, RuntimeError) as e:
        click.secho(f"\n[-] Fehler: {e}\n", fg="red")
        sys.exit(1)

    clean_discord = res["discord_name"]
    clean_username = res["username"]
    clean_role = res["role"]
    invite_link = res["invite_link"]
    dm_sent = res["dm_sent"]

    click.secho(f"\n[+] Einladung für '{clean_discord}' ({clean_username}) erfolgreich erstellt!", fg="green", bold=True)
    click.echo(f"    Rolle:            {clean_role}")
    if res.get("minecraft_name"):
        click.echo(f"    Minecraft IGN:    {res['minecraft_name']}")
    click.echo(f"    Einladungslink:   {invite_link}\n")

    if dm_sent:
        click.secho(f"[+] Discord DM erfolgreich an {clean_discord} zugestellt!\n", fg="green")
    else:
        if force_manual:
            click.secho(
                f"[!] Discord DM konnte nicht direkt zugestellt werden (Bot offline oder DM geschlossen).\n"
                f"    --force-manual angegeben: Bitte Einladungslink manuell teilen:\n"
                f"    👉 {invite_link}\n",
                fg="yellow",
            )
        else:
            click.secho(
                f"[-] Discord DM konnte nicht zugestellt werden (Privatsphäre-Einstellungen des Users blockieren DMs).\n"
                f"    Rollback: Bereinige angelegte Daten...",
                fg="red",
            )
            ldap = LDAPClient()
            authentik = AuthentikClient()
            ldap.delete_user(clean_username)
            authentik.delete_user(clean_username)
            authentik.delete_invitations(clean_username)
            click.secho(f"[-] Account '{clean_username}' wurde zurückgerollt. Bitte nutze --force-manual für manuelle Link-Weitergabe.\n", fg="red")
            sys.exit(1)


@cli.command("reset")
@click.argument("discord_name")
def reset_cmd(discord_name: str):
    """
    Generates a single-use Authentik recovery link for a user and sends via Discord DM.
    """
    clean_target = discord_name.strip()
    click.echo(f"[*] Processing password reset for '{clean_target}'...")

    ldap = LDAPClient()
    authentik = AuthentikClient()

    user = ldap.search_by_discord(clean_target) or ldap.get_user(clean_target)
    if not user:
        click.secho(f"[-] User '{clean_target}' not found in LDAP directory.", fg="red")
        sys.exit(1)

    username = user["uid"]
    click.echo(f"[*] Found user account: {username} (DN: {user['dn']})")

    try:
        recovery_link = authentik.create_recovery_link(username)
        if not recovery_link:
            click.secho(
                f"[-] Authentik could not generate recovery link for '{username}'.",
                fg="red",
            )
            sys.exit(1)

        click.echo("[+] Single-use Recovery Link generated successfully:")
        click.echo(f"    {recovery_link}\n")
    except Exception as e:
        click.secho(f"[-] Authentik recovery link generation failed: {e}", fg="red")
        sys.exit(1)

    dm_message = (
        f"🔐 **Passwort-Zurücksetzung für Weihnachtsdorf 7**\n\n"
        f"Hallo {username},\n"
        f"hier ist dein persönlicher Link zum Zurücksetzen deines Passworts:\n"
        f"👉 {recovery_link}\n\n"
        f"*Dieser Link verfällt nach einmaliger Nutzung.*"
    )
    sent = send_dm(clean_target, dm_message)
    if sent:
        click.secho(f"[+] Recovery link sent via Discord DM to {clean_target}!", fg="green")
    else:
        click.secho(
            f"[!] Discord DM could not be sent directly. Please send the recovery link manually.",
            fg="yellow",
        )


def resolve_notify_args(
    target: Optional[str],
    message: Optional[str],
    role_opt: Optional[str],
    user_opt: Optional[str],
    message_opt: Optional[str],
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Intelligently determines target recipients and message text across various CLI calling conventions.
    Returns: (role: Optional[str], user_target: Optional[str], text: str)
    """
    clean_target = (target or "").strip()
    clean_message = (message or "").strip()
    clean_msg_opt = (message_opt or "").strip()
    clean_role = (role_opt or "").strip().lower()
    clean_user = (user_opt or "").strip()

    role = None
    recipient = None
    text = ""

    # 1. If explicit --role option was provided
    if clean_role:
        role = clean_role
        if clean_msg_opt:
            text = clean_msg_opt
        elif clean_target and clean_message:
            text = f"{clean_target} {clean_message}"
        elif clean_target:
            text = clean_target
        elif clean_message:
            text = clean_message
        return role, None, text

    # 2. If explicit --user option was provided
    if clean_user:
        recipient = clean_user
        if clean_msg_opt:
            text = clean_msg_opt
        elif clean_target and clean_message:
            text = f"{clean_target} {clean_message}"
        elif clean_target:
            text = clean_target
        elif clean_message:
            text = clean_message
        return None, recipient, text

    # 3. Neither option provided: inspect positional arguments
    role_synonyms = {
        "admin": "admin",
        "admins": "admin",
        "mod": "mod",
        "mods": "mod",
        "player": "player",
        "players": "player",
        "staff": "staff",
        "all": "all",
    }
    if clean_target.lower() in role_synonyms:
        role = role_synonyms[clean_target.lower()]
        text = clean_msg_opt or clean_message
        return role, None, text

    recipient = clean_target or None
    text = clean_msg_opt or clean_message
    return None, recipient, text


@cli.command("notify")
@click.argument("target", required=False)
@click.argument("message", required=False)
@click.option(
    "--role",
    "-r",
    type=click.Choice(["admin", "mod", "player", "staff", "all"], case_sensitive=False),
    help="Target an entire role: admin, mod, player, staff (admins+mods), or all.",
)
@click.option(
    "--user",
    "-u",
    help="Target a specific user (Discord username, Discord ID, or LDAP username).",
)
@click.option(
    "--message",
    "-m",
    "message_opt",
    help="The message text to send.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt for role broadcasts.",
)
def notify_cmd(
    target: Optional[str],
    message: Optional[str],
    role: Optional[str],
    user: Optional[str],
    message_opt: Optional[str],
    yes: bool,
):
    """
    Sends a Discord notification message to a specific user or an entire role group.

    Examples:
      wd-admin notify --role admin "Hallo Team, hier ist das Git: https://github.com/..."
      wd-admin notify nummernfeld "Hier ist dein Link: https://..."
      wd-admin notify staff -m "Wartungsarbeiten um 18:00 Uhr"
    """
    resolved_role, resolved_user, text = resolve_notify_args(
        target, message, role, user, message_opt
    )

    if not text:
        click.secho("[-] Error: Please provide a message to send.", fg="red")
        sys.exit(1)

    if not resolved_role and not resolved_user:
        click.secho(
            "[-] Error: Please specify a recipient (username/Discord) or a role group (--role admin|mod|player|staff|all).",
            fg="red",
        )
        sys.exit(1)

    ldap = LDAPClient()

    # Case 1: Role broadcast
    if resolved_role:
        admins, mods = ldap.get_admins_and_mods()
        players = ldap.get_players()

        if resolved_role == "admin":
            recipients = admins
        elif resolved_role == "mod":
            recipients = mods
        elif resolved_role == "staff":
            recipients = admins + mods
        elif resolved_role == "player":
            recipients = players
        elif resolved_role == "all":
            recipients = admins + mods + players
        else:
            recipients = []

        if not recipients:
            click.secho(f"[!] No active accounts found for role '{resolved_role}'.", fg="yellow")
            return

        click.secho(
            f"\n[*] Broadcasting Discord notification to role '{resolved_role}' ({len(recipients)} recipient(s)):",
            bold=True,
        )
        for r in recipients:
            d_handle = r.get("discordId") or r.get("discordName") or r.get("uid")
            click.echo(f"    • {r.get('uid')} (Discord: {d_handle})")

        click.echo(f"\n[*] Message preview:\n---\n{text}\n---")

        if not yes and sys.stdin.isatty():
            if not click.confirm("\nProceed with sending to all listed recipients?", default=True):
                click.echo("[*] Notification broadcast aborted.")
                return

        click.echo("\n[*] Dispatching messages...")
        success_count = 0
        fail_count = 0

        for r in recipients:
            uid = r.get("uid")
            discord_handle = r.get("discordId") or r.get("discordName") or uid
            click.echo(f"  -> Sending to {uid} ({discord_handle})... ", nl=False)
            sent = send_dm(discord_handle, text)
            if sent:
                click.secho("OK", fg="green")
                success_count += 1
            else:
                click.secho("FAILED", fg="red")
                fail_count += 1
            if len(recipients) > 1:
                time.sleep(0.5)

        click.echo("")
        if fail_count == 0:
            click.secho(
                f"[+] All {success_count} notifications successfully delivered via Discord DM!",
                fg="green",
                bold=True,
            )
        else:
            click.secho(
                f"[!] Delivery finished: {success_count} succeeded, {fail_count} failed.",
                fg="yellow",
                bold=True,
            )
        return

    # Case 2: Single user
    clean_target = resolved_user.strip()
    click.echo(f"[*] Resolving recipient '{clean_target}' in directory...")
    user_entry = ldap.search_by_discord(clean_target) or ldap.get_user(clean_target)

    if user_entry:
        discord_handle = (
            user_entry.get("discordId")
            or user_entry.get("discordName")
            or user_entry.get("uid")
        )
        display_name = f"{user_entry.get('uid')} (Discord: {discord_handle})"
    else:
        discord_handle = clean_target
        display_name = clean_target

    click.echo(f"[*] Dispatching message to {display_name}...")
    sent = send_dm(discord_handle, text)
    if sent:
        click.secho(f"[+] Message successfully delivered to {display_name}!", fg="green", bold=True)
    else:
        click.secho(
            f"[-] Could not deliver Discord message to {display_name}. "
            f"(Bot offline, token missing, or user has DMs disabled).",
            fg="red",
        )
        sys.exit(1)


@cli.command("list")
def list_cmd():
    """
    Lists all active admins, moderators, and registered players with their linked Discord and Minecraft identities.
    """
    ldap = LDAPClient()
    mc = MinecraftSyncClient()
    is_live = mc.is_project_launched()

    admins, mods = ldap.get_admins_and_mods()
    players = ldap.get_players()

    total_staff = len(admins) + len(mods)
    click.secho(
        f"\n=== Weihnachtsdorf 7 Active Staff ({total_staff} members) ===\n",
        bold=True,
    )

    fmt = "{:<14} {:<8} {:<18} {:<18} {:<36}"
    header = fmt.format("USERNAME", "ROLE", "DISCORD", "MINECRAFT IGN", "MINECRAFT UUID")
    click.secho(header, bold=True)
    click.echo("-" * 100)

    for a in admins:
        click.secho(
            fmt.format(
                a["uid"],
                "admin",
                a.get("discordName") or "-",
                a.get("minecraftName") or "-",
                a.get("minecraftUuid") or "-",
            ),
            fg="red",
        )

    for m in mods:
        click.secho(
            fmt.format(
                m["uid"],
                "mod",
                m.get("discordName") or "-",
                m.get("minecraftName") or "-",
                m.get("minecraftUuid") or "-",
            ),
            fg="yellow",
        )

    # Players section
    status_tag = "[LIVE - WHITELISTED]" if is_live else "[PRE-LAUNCH - STAGED]"
    click.secho(
        f"\n=== Weihnachtsdorf 7 Registered Players ({len(players)} players) • Whitelist Gate: {status_tag} ===\n",
        bold=True,
    )
    p_fmt = "{:<14} {:<18} {:<18} {:<36} {:<16}"
    p_header = p_fmt.format("USERNAME", "DISCORD", "MINECRAFT IGN", "MINECRAFT UUID", "WHITELIST")
    click.secho(p_header, bold=True)
    click.echo("-" * 106)

    if not players:
        click.echo("  (Keine regulären Spieler registriert)")
    else:
        for p in players:
            ign = p.get("minecraftName")
            if not ign:
                wl_status = "No IGN"
                color = "yellow"
            elif is_live:
                wl_status = "WHITELISTED"
                color = "green"
            else:
                wl_status = "PRE-REGISTERED"
                color = "cyan"

            click.secho(
                p_fmt.format(
                    p["uid"],
                    p.get("discordName") or "-",
                    ign or "-",
                    p.get("minecraftUuid") or "-",
                    wl_status,
                ),
                fg=color,
            )

    click.echo("")


@cli.group("project")
def project_group():
    """Manage Weihnachtsdorf 7 project launch state and player whitelist gate."""
    pass


@project_group.command("status")
def project_status_cmd():
    """Displays current launch mode, whitelist gate, and player counts."""
    ldap = LDAPClient()
    mc = MinecraftSyncClient()
    is_live = mc.is_project_launched()
    admins, mods = ldap.get_admins_and_mods()
    players = ldap.get_players()

    click.secho("\n=== Weihnachtsdorf 7 Project Status ===", bold=True)
    if is_live:
        click.secho("Status:       🔴 LIVE (Server geöffnet für alle registrierten Spieler)", fg="green", bold=True)
        click.secho("BlueMap Gate: 🟢 FREIGESCHALTET (Für alle Spieler zugänglich)", fg="green")
    else:
        click.secho("Status:       🟡 PRE-LAUNCH (Wartungsmodus: Nur Staff hat Whitelist-Zugang)", fg="yellow", bold=True)
        click.secho("BlueMap Gate: 🔒 GESPERRT (Pre-Launch Schutz: Nur Staff hat Karten-Zugriff)", fg="yellow")

    click.echo(f"Admins:       {len(admins)}")
    click.echo(f"Moderatoren:  {len(mods)}")
    click.echo(f"Spieler:      {len(players)} registriert")

    staged_with_ign = sum(1 for p in players if p.get("minecraftName"))
    click.echo(f"Spieler bereit (mit IGN): {staged_with_ign}/{len(players)}")
    click.echo("")


@project_group.command("launch")
def project_launch_cmd():
    """Launches the project and whitelists all registered players immediately."""
    mc = MinecraftSyncClient()
    click.secho("[*] Triggering Weihnachtsdorf 7 PROJECT LAUNCH...", bold=True)
    report = mc.launch_project()
    if report["success"]:
        click.secho(
            f"\n🎉 [SUCCESS] Weihnachtsdorf 7 is now LIVE! "
            f"{len(report.get('synced_players', []))} player(s) whitelisted on Minecraft server.",
            fg="green",
            bold=True,
        )
        click.secho("    [BlueMap Gate] Freigeschaltet für alle registrierten Spieler.", fg="green")
        for p in report.get("synced_players", []):
            click.echo(f"    [Player Whitelisted] {p['uid']} -> IGN: {p['ign']}")
        click.echo("")
        try:
            log_admin_event(
                title="🚀 Weihnachtsdorf 7 ist LIVE!",
                description=f"Projekt wurde gestartet. {len(report.get('synced_players', []))} Spieler freigeschaltet (Whitelist & BlueMap).",
                color=COLOR_SUCCESS,
                author_name="wd-admin CLI",
            )
            from wd_identity.playerlist import sync_playerlist_rest
            sync_playerlist_rest(force=True)
        except Exception:
            pass
    else:
        click.secho("[-] Failed to launch project: Minecraft Wings API error.", fg="red")
        sys.exit(1)


@project_group.command("lock")
def project_lock_cmd():
    """Locks the server into pre-launch mode (removes players from whitelist, staff remains)."""
    mc = MinecraftSyncClient()
    click.secho("[*] Locking Weihnachtsdorf 7 into PRE-LAUNCH / MAINTENANCE mode...", bold=True)
    report = mc.lock_project()
    if report["success"]:
        click.secho(
            f"\n🔒 [LOCKED] Project locked into pre-launch mode. "
            f"Removed normal players from whitelist; staff members remain active.",
            fg="yellow",
            bold=True,
        )
        click.secho("    [BlueMap Gate] Gesperrt für reguläre Spieler (Pre-Launch Schutz aktiv).", fg="yellow")
        click.echo("")
        try:
            log_admin_event(
                title="🔒 Projekt in Wartungsmodus versetzt (Pre-Launch Lock)",
                description="Projekt wurde gesperrt. Normale Spieler wurden von der Whitelist entfernt und auf BlueMap gesperrt; Staff bleibt aktiv.",
                color=COLOR_WARNING,
                author_name="wd-admin CLI",
            )
            from wd_identity.playerlist import sync_playerlist_rest
            sync_playerlist_rest(force=True)
        except Exception:
            pass
    else:
        click.secho("[-] Failed to lock project: Minecraft Wings API error.", fg="red")
        sys.exit(1)


@cli.command("revoke")
@click.argument("target")
@click.option(
    "--keep-ldap",
    is_flag=True,
    default=False,
    help="Keep the LDAP entry and only remove group memberships and external access.",
)
def revoke_cmd(target: str, keep_ldap: bool):
    """
    Completely revokes access and purges all user data across all systems:
    - OpenLDAP: Removes memberships from wd_admins, wd_mods, wd_players (and deletes LDAP account unless --keep-ldap)
    - Authentik IdP: Deletes user account, invalidates sessions, and removes pending invitations
    - Planka Kanban: Terminates active sessions, clears memberships, and deletes user account
    - Minecraft Server: Kicks player if online, removes whitelist, clears LuckPerms permissions
    - SQLite DB: Lifts active bans and cancels pending applications
    TARGET can be a Discord name, Discord ID, LDAP username, or Minecraft IGN.
    """
    clean_target = target.strip()
    click.echo(f"[*] Starting full purge & revocation for '{clean_target}'...")

    ldap = LDAPClient()
    planka = PlankaSyncClient()
    mc = MinecraftSyncClient()
    authentik = AuthentikClient()

    # Resolve user metadata across LDAP
    user = None
    if clean_target.isdigit():
        user = ldap.search_by_discord(clean_target)
    if not user:
        user = (
            ldap.get_user(clean_target)
            or ldap.search_by_discord(clean_target)
            or ldap.search_by_minecraft(clean_target)
        )

    if user:
        username = user["uid"]
        discord_id = user.get("discordId")
        discord_name = user.get("discordName")
        ign = user.get("minecraftName")
        mc_uuid = user.get("minecraftUuid")
        click.echo(f"[+] Found LDAP user: {username} (IGN: {ign or 'None'}, Discord: {discord_name or discord_id or 'None'})")
    else:
        username = clean_target
        ign = clean_target
        discord_id = clean_target if clean_target.isdigit() else None
        discord_name = clean_target
        mc_uuid = None
        click.echo(f"[!] Target not found in LDAP; proceeding to clean Authentik, Planka, Minecraft, and DB by name '{clean_target}'...")

    # 1. OpenLDAP Clean Up
    if user:
        if keep_ldap:
            from wd_identity.membership import revoke_user_privileges
            revoke_user_privileges(user, reason="Admin Revoke (--keep-ldap)", initiator="wd-admin CLI")
            click.secho(
                f"\n[+] Alle Berechtigungen für '{username}' wurden erfolgreich entzogen!\n"
                f"    • OpenLDAP: Gruppen wd_admins, wd_mods, wd_players entfernt (Account bleibt erhalten)\n"
                f"    • Authentik: Deaktiviert & BlueMap gesperrt (Account bleibt erhalten)\n"
                f"    • Planka: Deaktiviert (Account bleibt erhalten)\n"
                f"    • Minecraft: Whitelist & LuckPerms entfernt\n"
                f"    • Spielerliste: Aktualisiert\n",
                fg="green",
            )
            return
        else:
            ldap_ok = ldap.delete_user(username)
            click.echo(f"[+] OpenLDAP: Account '{username}' deleted: {'OK' if ldap_ok else 'FAILED'}")
    else:
        click.echo("[-] OpenLDAP: No LDAP entry to delete.")

    # 2. Authentik IdP Clean Up
    auth_user_del = authentik.delete_user(username)
    click.echo(f"[+] Authentik: User '{username}' deleted: {'OK' if auth_user_del else 'Not found or failed'}")
    inv_del_count = authentik.delete_invitations(username)
    if discord_name and discord_name != username:
        inv_del_count += authentik.delete_invitations(discord_name)
    click.echo(f"[+] Authentik: Pending invitations deleted: {inv_del_count}")

    # 3. Planka Kanban Clean Up
    planka_del = planka.delete_user(username)
    click.echo(f"[+] Planka: User and sessions purged: {'OK' if planka_del else 'FAILED'}")

    # 4. Minecraft Server Clean Up
    if ign:
        mc_ok = mc.revoke_player_privileges(ign, mc_uuid=mc_uuid)
        target_lp = mc_uuid if mc_uuid else ign
        mc.execute_commands([
            f"lp user {target_lp} clear",
            f'kick {ign} "Zugang wurde widerrufen."',
        ])
        click.echo(f"[+] Minecraft Server: Whitelist removed, kicked & LuckPerms cleared for '{ign}': {'OK' if mc_ok else 'FAILED'}")
        mc.sync_all(force=True)
    else:
        click.echo("[-] Minecraft Server: No IGN specified; skipping.")

    # 5. SQLite DB (Applications & Bans) Clean Up
    lift_count = applications.lift_ban(discord_id=discord_id, minecraft_ign=ign)
    click.echo(f"[+] SQLite: Active bans lifted: {lift_count}")
    if discord_id:
        status = applications.get_user_status(discord_id)
        if status.get("has_pending") and status.get("pending_app"):
            applications.reject_application(
                status["pending_app"]["id"],
                "cli-admin",
                "Admin (Revoke)",
                "Zugang widerrufen (Account purged)",
            )
            click.echo(f"[+] SQLite: Pending application #{status['pending_app']['id']} cancelled.")

    click.secho(f"\n[+] Komplettes Cleanup erfolgreich abgeschlossen für '{clean_target}'!\n", fg="green")
    try:
        log_admin_event(
            title="🗑️ Zugang widerrufen (Purge)",
            description=f"Der Zugang für **{clean_target}** wurde vollständig widerrufen und alle Berechtigungen bereinigt.",
            color=COLOR_ERROR,
            fields=[
                {"name": "Ziel", "value": clean_target, "inline": True},
                {"name": "LDAP erhalten", "value": "Ja" if keep_ldap else "Nein (Gelöscht)", "inline": True},
            ],
            author_name="wd-admin CLI",
        )
        from wd_identity.playerlist import sync_playerlist_rest
        sync_playerlist_rest(force=True)
    except Exception:
        pass


@cli.command("ban")
@click.argument("target")
@click.option("--reason", "-m", default="Regelverstoß", help="Begründung für die Sperre.")
def ban_cmd(target: str, reason: str):
    """
    Bans a player from Minecraft server and blocks BlueMap access.
    Leaves Authentik account active for password recovery and communication.
    TARGET can be a Discord name, Discord ID, username, or Minecraft IGN.
    """
    clean_target = target.strip()
    click.echo(f"[*] Verhängen der Sperre für '{clean_target}' (Grund: {reason})...")

    ldap = LDAPClient()
    mc = MinecraftSyncClient()
    authentik = AuthentikClient()

    user = None
    if clean_target.isdigit():
        user = ldap.search_by_discord(clean_target)
    if not user:
        user = ldap.get_user(clean_target) or ldap.search_by_discord(clean_target) or ldap.search_by_minecraft(clean_target)

    discord_id = None
    ign = None
    uid = None

    if user:
        discord_id = user.get("discordId")
        ign = user.get("minecraftName")
        uid = user["uid"]
    else:
        ign = clean_target

    mc_ok = False
    if ign:
        mc_ok = mc.ban_player(ign, reason)
        click.echo(f"[*] Minecraft server ban & whitelist removal for '{ign}': {'OK' if mc_ok else 'FAILED'}")
    else:
        click.echo("[!] No Minecraft IGN linked; skipping Minecraft server ban.")

    bm_ok = False
    if uid:
        bm_ok = authentik.set_user_banned(uid, True)
        click.echo(f"[*] Authentik BlueMap access blocked for '{uid}': {'OK' if bm_ok else 'FAILED'}")

    applications.record_ban(
        discord_id=discord_id,
        minecraft_ign=ign,
        banned_by="wd-admin-cli",
        reason=reason,
    )
    click.secho(f"\n[+] Spieler '{clean_target}' erfolgreich gesperrt!\n", fg="green")
    try:
        log_admin_event(
            title="🔨 Spieler gesperrt",
            description=f"Spieler **{clean_target}** wurde gesperrt.",
            color=COLOR_ERROR,
            fields=[
                {"name": "Ziel", "value": clean_target, "inline": True},
                {"name": "Grund", "value": reason, "inline": True},
                {"name": "Minecraft Ban", "value": "✅ Ausgeführt" if (ign and mc_ok) else "⚠️ Kein IGN / Fehler", "inline": True},
                {"name": "BlueMap Ban", "value": "✅ Gesperrt" if bm_ok else "⚠️ Kein Account", "inline": True},
            ],
            author_name="wd-admin CLI",
        )
        from wd_identity.playerlist import sync_playerlist_rest
        sync_playerlist_rest(force=True)
    except Exception:
        pass


@cli.command("pardon")
@click.argument("target")
def pardon_cmd(target: str):
    """
    Lifts ban for a player on Minecraft and restores BlueMap access.
    """
    clean_target = target.strip()
    click.echo(f"[*] Aufheben der Sperre für '{clean_target}'...")

    ldap = LDAPClient()
    mc = MinecraftSyncClient()
    authentik = AuthentikClient()

    user = None
    if clean_target.isdigit():
        user = ldap.search_by_discord(clean_target)
    if not user:
        user = ldap.get_user(clean_target) or ldap.search_by_discord(clean_target) or ldap.search_by_minecraft(clean_target)

    discord_id = None
    ign = None
    uid = None

    if user:
        discord_id = user.get("discordId")
        ign = user.get("minecraftName")
        uid = user["uid"]
    else:
        ign = clean_target

    if ign:
        mc_ok = mc.pardon_player(ign)
        click.echo(f"[*] Minecraft server pardon for '{ign}': {'OK' if mc_ok else 'FAILED'}")

    if uid:
        bm_ok = authentik.set_user_banned(uid, False)
        click.echo(f"[*] Authentik BlueMap access restored for '{uid}': {'OK' if bm_ok else 'FAILED'}")

    count = applications.lift_ban(discord_id=discord_id, minecraft_ign=ign)
    click.echo(f"[*] Active ban records lifted in database: {count}")
    click.secho(f"\n[+] Spieler '{clean_target}' erfolgreich entbannt!\n", fg="green")
    try:
        log_admin_event(
            title="✨ Spieler entbannt",
            description=f"Sperre für **{clean_target}** wurde aufgehoben.",
            color=COLOR_SUCCESS,
            fields=[{"name": "Ziel", "value": clean_target}],
            author_name="wd-admin CLI",
        )
        from wd_identity.playerlist import sync_playerlist_rest
        sync_playerlist_rest(force=True)
    except Exception:
        pass


@cli.group("apps")
def apps_group():
    """Manage candidate applications and unban appeals."""
    pass


@apps_group.command("list")
@click.option("--status", type=click.Choice(["pending", "accepted", "rejected"], case_sensitive=False))
@click.option("--type", "app_type", type=click.Choice(["initial", "unban"], case_sensitive=False))
def list_apps_cmd(status: Optional[str], app_type: Optional[str]):
    """Lists applications."""
    items = applications.list_applications(status=status, app_type=app_type)
    if not items:
        click.echo("Keine Anträge gefunden.")
        return

    click.secho(f"\n{'ID':<5} {'TYP':<9} {'STATUS':<10} {'DISCORD USER':<25} {'DATUM':<20}", fg="cyan")
    click.echo("-" * 75)
    for it in items:
        color = "yellow" if it["status"] == "pending" else ("green" if it["status"] == "accepted" else "red")
        click.secho(
            f"#{it['id']:<4} {it['app_type']:<9} {it['status']:<10} {it['discord_username']:<25} {it['created_at'][:19]:<20}",
            fg=color,
        )
        msg_preview = it["message"].replace("\n", " ")[:60]
        click.echo(f"      Nachricht: {msg_preview}...")
    click.echo("")


@apps_group.command("accept")
@click.argument("app_id", type=int)
def accept_app_cmd(app_id: int):
    """Accepts an application by ID."""
    app = applications.get_application(app_id)
    if not app:
        click.secho(f"[-] Antrag #{app_id} nicht gefunden.", fg="red")
        sys.exit(1)

    updated = applications.accept_application(app_id, "cli-admin", "Admin (CLI)")
    click.secho(f"[+] Antrag #{app_id} ({updated['discord_username']}) wurde angenommen!", fg="green")


@apps_group.command("reject")
@click.argument("app_id", type=int)
@click.option("--reason", "-m", help="Begründung der Ablehnung.")
@click.option("--cooldown-days", "-c", type=int, help="Cooldown in Tagen (-1 für permanent).")
def reject_app_cmd(app_id: int, reason: Optional[str], cooldown_days: Optional[int]):
    """Rejects an application by ID with optional cooldown."""
    app = applications.get_application(app_id)
    if not app:
        click.secho(f"[-] Antrag #{app_id} nicht gefunden.", fg="red")
        sys.exit(1)

    updated = applications.reject_application(
        app_id=app_id,
        reviewer_id="cli-admin",
        reviewer_name="Admin (CLI)",
        reason=reason,
        cooldown_days=cooldown_days,
    )
    click.secho(f"[+] Antrag #{app_id} ({updated['discord_username']}) wurde abgelehnt (Sperre: {cooldown_days} Tage)!", fg="red")


@cli.command("sync")
@click.option("--force", "-f", is_flag=True, help="Force execution of Minecraft commands even if state is unchanged.")
def sync_cmd(force: bool):
    """
    Triggers synchronization of Minecraft server whitelist and LuckPerms roles.
    """
    click.echo(f"[*] Triggering Minecraft server synchronization (force={force})...")
    mc = MinecraftSyncClient()
    report = mc.sync_all(force=force)

    if report["success"]:
        synced_players = report.get("synced_players", [])
        staged_players = report.get("staged_players", [])
        mode_str = "LIVE" if report.get("is_launched") else "PRE-LAUNCH"

        if report["commands_executed"] > 0:
            click.secho(
                f"[+] Synchronization complete [{mode_str}]: {len(report['synced_admins'])} admins, "
                f"{len(report['synced_mods'])} mods, {len(synced_players)} players whitelisted, "
                f"{len(report['revoked_users'])} revoked ({report['commands_executed']} commands sent).",
                fg="green",
            )
            for a in report["synced_admins"]:
                click.echo(f"    [Admin Updated]  {a['uid']} -> IGN: {a['ign']}")
            for m in report["synced_mods"]:
                click.echo(f"    [Mod Updated]    {m['uid']} -> IGN: {m['ign']}")
            for p in synced_players:
                click.echo(f"    [Player Whitelist] {p['uid']} -> IGN: {p['ign']}")
            for r in report["revoked_users"]:
                click.secho(f"    [Revoked]        {r['uid']} -> IGN: {r['ign']}", fg="red")
        else:
            click.secho(
                f"[+] Synchronization up-to-date [{mode_str}]: {len(report['unchanged_users'])} accounts in sync. "
                f"({len(staged_players)} player(s) staged for launch). No console commands required.",
                fg="green",
            )
        for s in report["skipped_users"]:
            click.secho(f"    [Skip]  {s['uid']} ({s['reason']})", fg="yellow")
    else:
        click.secho("[-] Synchronization encountered errors.", fg="red")
        sys.exit(1)


if __name__ == "__main__":
    cli()
