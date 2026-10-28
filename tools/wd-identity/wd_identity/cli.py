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

from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.discord_bot import send_dm
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient
from wd_identity.mojang_client import get_player_profile
from wd_identity.planka_sync import PlankaSyncClient
from wd_identity.utils import sanitize_email, sanitize_username, validate_minecraft_ign


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
def invite_cmd(discord_name: str, role: str, ign: Optional[str], username: Optional[str]):
    """
    Invites a new player, mod, or admin to Weihnachtsdorf 7.
    Creates LDAP entry, generates Authentik enrollment link, and sends Discord DM.
    """
    clean_role = role.lower().strip()
    clean_discord = discord_name.strip()
    clean_username = (
        sanitize_username(username)
        if username
        else sanitize_username(clean_discord)
    )
    user_email = sanitize_email(clean_username)

    click.echo(f"[*] Preparing invitation for '{clean_discord}' (Role: {clean_role}, UID: {clean_username})...")

    ldap = LDAPClient()
    authentik = AuthentikClient()

    # Resolve Minecraft IGN if provided
    canonical_ign = None
    mc_uuid = None
    if ign:
        raw_ign = ign.strip()
        if not validate_minecraft_ign(raw_ign):
            click.secho(f"[-] Invalid Minecraft IGN: '{raw_ign}'. Must be 1-16 alphanumeric chars or underscores.", fg="red")
            sys.exit(1)
        try:
            canonical_ign, mc_uuid = get_player_profile(raw_ign)
            click.echo(f"[*] Resolved Minecraft IGN: {canonical_ign} ({mc_uuid})")
        except Exception as e:
            click.secho(f"[-] Minecraft IGN resolution failed: {e}", fg="red")
            sys.exit(1)

    # 1. Create / Update LDAP Entry
    discord_id = clean_discord if clean_discord.isdigit() else None
    try:
        user_entry = ldap.create_or_update_user(
            username=clean_username,
            discord_name=clean_discord,
            discord_id=discord_id,
            email=user_email,
            role=clean_role,
            minecraft_name=canonical_ign,
            minecraft_uuid=mc_uuid,
        )
        click.echo(f"[+] LDAP entry configured: {user_entry.get('dn', clean_username)}")
    except Exception as e:
        click.secho(f"[-] Failed to configure LDAP entry: {e}", fg="red")
        sys.exit(1)

    # 2. Trigger Authentik LDAP sync so user exists in Authentik
    authentik.trigger_ldap_sync()

    # 3. Generate Authentik Invitation Link
    fixed_data = {
        "username": clean_username,
        "email": user_email,
        "role": clean_role,
        "discord_name": clean_discord,
    }
    if discord_id:
        fixed_data["discord_id"] = discord_id
    if canonical_ign:
        fixed_data["minecraft_name"] = canonical_ign

    try:
        invite_link = authentik.create_invitation(
            name=f"invite-{clean_username}",
            fixed_data=fixed_data,
        )
        click.echo("[+] Single-use Authentik Enrollment Link generated successfully:")
        click.echo(f"    {invite_link}\n")
    except Exception as e:
        click.secho(f"[-] Failed to generate Authentik enrollment link: {e}", fg="red")
        sys.exit(1)

    # 4. Dispatch Discord DM
    if clean_role == "player":
        dm_message = (
            f"🎅 **Einladung zum Weihnachtsdorf 7!** 🎄\n\n"
            f"Du wurdest als **Spieler** eingeladen.\n"
            f"Bitte schließe deine Vorab-Registrierung über folgenden Link ab (Passwort & 2FA-Einrichtung):\n"
            f"👉 {invite_link}\n\n"
            f"*Nach der Registrierung ist dein Account startklar. Die Freischaltung auf dem Server erfolgt zum Projektstart!*\n"
            f"*Dieser Link ist einmalig gültig und verfällt in 24 Stunden.*"
        )
    else:
        dm_message = (
            f"🎅 **Einladung zum Weihnachtsdorf 7 Team!** 🎄\n\n"
            f"Du wurdest als **{clean_role.upper()}** eingeladen.\n"
            f"Bitte schließe deine Registrierung über folgenden Link ab (Passwort & 2FA-Einrichtung):\n"
            f"👉 {invite_link}\n\n"
            f"*Dieser Link ist einmalig gültig und verfällt in 24 Stunden.*"
        )
    sent = send_dm(clean_discord, dm_message)
    if sent:
        click.secho(f"[+] Discord DM successfully dispatched to {clean_discord}!", fg="green")
    else:
        click.secho(
            f"[!] Discord DM could not be sent directly (bot offline or DM closed). "
            f"Please share the enrollment link manually.",
            fg="yellow",
        )


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
    else:
        click.secho("Status:       🟡 PRE-LAUNCH (Wartungsmodus: Nur Staff hat Whitelist-Zugang)", fg="yellow", bold=True)

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
        for p in report.get("synced_players", []):
            click.echo(f"    [Player Whitelisted] {p['uid']} -> IGN: {p['ign']}")
        click.echo("")
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
        click.echo("")
    else:
        click.secho("[-] Failed to lock project: Minecraft Wings API error.", fg="red")
        sys.exit(1)


@cli.command("revoke")
@click.argument("discord_name")
def revoke_cmd(discord_name: str):
    """
    Revokes admin/mod privileges:
    - Removes LDAP group memberships (wd_admins / wd_mods)
    - Deactivates user account in Planka Kanban
    - Revokes Minecraft whitelist and LuckPerms group permissions
    """
    clean_target = discord_name.strip()
    click.echo(f"[*] Revoking privileges for '{clean_target}'...")

    ldap = LDAPClient()
    planka = PlankaSyncClient()
    mc = MinecraftSyncClient()

    user = ldap.search_by_discord(clean_target) or ldap.get_user(clean_target)
    if not user:
        click.secho(f"[-] User '{clean_target}' not found in LDAP.", fg="red")
        sys.exit(1)

    username = user["uid"]
    ign = user.get("minecraftName")
    mc_uuid = user.get("minecraftUuid")

    # 1. Remove from LDAP groups
    ldap_ok = ldap.revoke_roles(username)
    click.echo(f"[*] LDAP group memberships removed: {'OK' if ldap_ok else 'FAILED'}")

    # 2. Deactivate in Planka
    planka_ok = planka.deactivate_user(username)
    click.echo(f"[*] Planka user account deactivated: {'OK' if planka_ok else 'FAILED'}")

    # 3. Revoke Minecraft access
    mc_ok = False
    if ign:
        mc_ok = mc.revoke_player_privileges(ign, mc_uuid=mc_uuid)
        click.echo(f"[*] Minecraft whitelist & LuckPerms removed for '{ign}': {'OK' if mc_ok else 'FAILED'}")
    else:
        click.echo("[*] No Minecraft IGN linked; skipping Minecraft server revocation.")

    # 4. Trigger sync to persist revocation in state
    mc.sync_all(force=True)

    click.secho(f"\n[+] Privileges successfully revoked for {username}!\n", fg="green")


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
