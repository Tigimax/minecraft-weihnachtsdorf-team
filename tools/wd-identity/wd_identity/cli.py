"""
Command-line interface (wd-admin) for Weihnachtsdorf 7.
Commands:
- invite <discord_name> [--role admin|mod] [--ign <minecraft_ign>]
- reset <discord_name>
- list
- revoke <discord_name>
- sync
"""

import sys
from typing import Optional
import click

from wd_identity.authentik_client import AuthentikClient
from wd_identity.config import settings
from wd_identity.discord_bot import send_dm
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient
from wd_identity.mojang_client import get_player_profile
from wd_identity.planka_sync import PlankaSyncClient


@click.group()
def cli():
    """Weihnachtsdorf 7 Identity Administration Tool (wd-admin)"""
    pass


@cli.command("invite")
@click.argument("discord_name")
@click.option(
    "--role",
    "-r",
    type=click.Choice(["admin", "mod"], case_sensitive=False),
    default="admin",
    help="Role to assign (admin or mod). Defaults to admin.",
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
    Invites a new admin or moderator to Weihnachtsdorf 7.
    Creates LDAP entry, generates Authentik enrollment link, and sends Discord DM.
    """
    clean_role = role.lower()
    clean_discord = discord_name.strip()
    clean_username = (
        username.strip().lower()
        if username
        else clean_discord.lower().replace("#", "_").replace(" ", "_")
    )

    click.echo(f"[*] Preparing invitation for '{clean_discord}' (Role: {clean_role})...")

    ldap = LDAPClient()
    authentik = AuthentikClient()

    # Resolve Minecraft IGN if provided
    canonical_ign = None
    mc_uuid = None
    if ign:
        canonical_ign, mc_uuid = get_player_profile(ign)
        click.echo(f"[*] Resolved Minecraft IGN: {canonical_ign} ({mc_uuid})")

    # 1. Create / Update LDAP Entry
    discord_id = clean_discord if clean_discord.isdigit() else None
    user_entry = ldap.create_or_update_user(
        username=clean_username,
        discord_name=clean_discord,
        discord_id=discord_id,
        role=clean_role,
        minecraft_name=canonical_ign,
        minecraft_uuid=mc_uuid,
    )
    click.echo(f"[+] LDAP entry configured: {user_entry.get('dn', clean_username)}")

    # 2. Generate Authentik Invitation Link
    try:
        invite_link = authentik.create_invitation(name=f"invite-{clean_username}")
        click.echo("[+] Single-use Authentik Enrollment Link generated successfully:")
        click.echo(f"    {invite_link}\n")
    except Exception as e:
        click.secho(f"[-] Failed to generate Authentik enrollment link: {e}", fg="red")
        sys.exit(1)

    # 3. Dispatch Discord DM
    dm_message = (
        f"🎅 **Einladung zum Weihnachtsdorf 7 Team!** 🎄\n\n"
        f"Du wurdest als **{clean_role.upper()}** eingeladen.\n"
        f"Bitte schließe deine Registrierung über folgenden Link ab (Passwort & 2FA-Einrichtung):\n"
        f"👉 {invite_link}\n\n"
        f"*Dieser Link ist einmalig gültig und verfällt in 48 Stunden.*"
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


@cli.command("list")
def list_cmd():
    """
    Lists all active admins and moderators with their linked Discord and Minecraft identities.
    """
    ldap = LDAPClient()
    admins, mods = ldap.get_admins_and_mods()

    total = len(admins) + len(mods)
    click.secho(
        f"\n=== Weihnachtsdorf 7 Active Staff ({total} members) ===\n",
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

    click.echo("")


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

    click.secho(f"\n[+] Privileges successfully revoked for {username}!\n", fg="green")


@cli.command("sync")
def sync_cmd():
    """
    Triggers an immediate synchronization of Minecraft server whitelist and LuckPerms roles.
    """
    click.echo("[*] Triggering Minecraft server synchronization...")
    mc = MinecraftSyncClient()
    report = mc.sync_all()

    if report["success"]:
        click.secho(
            f"[+] Synchronization complete: {len(report['synced_admins'])} admins, "
            f"{len(report['synced_mods'])} mods synced.",
            fg="green",
        )
        for a in report["synced_admins"]:
            click.echo(f"    [Admin] {a['uid']} -> IGN: {a['ign']}")
        for m in report["synced_mods"]:
            click.echo(f"    [Mod]   {m['uid']} -> IGN: {m['ign']}")
        for s in report["skipped_users"]:
            click.secho(f"    [Skip]  {s['uid']} ({s['reason']})", fg="yellow")
    else:
        click.secho("[-] Synchronization encountered errors.", fg="red")
        sys.exit(1)


if __name__ == "__main__":
    cli()
