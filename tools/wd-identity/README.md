# wd-identity

Weihnachtsdorf 7 Identity Management, SSO & Minecraft Synchronization Package.

## Features
- **OpenLDAP Integration**: User management, custom `wdAccount` schema (`discordName`, `discordId`, `minecraftName`, `minecraftUuid`), and group mappings (`cn=wd_admins`, `cn=wd_mods`).
- **Authentik SSO**: Automated invitation enrollment links with mandatory TOTP 2FA, password recovery links.
- **Minecraft Server Privilege Sync**: Integration with Node 2 Wings API to manage `whitelist` and LuckPerms group assignments (`admin`, `mod`).
- **Planka Kanban Provisioning**: Automatic database synchronization via SSH into Postgres container.
- **Discord Bot**: Slash commands (`/passwort-reset`, `/status`, `/links`), automated onboarding and welcome guide DMs.
- **CLI Utilities**:
  - `wd-admin invite <discord_name> [--role admin|mod] [--ign <ign>]`
  - `wd-admin reset <discord_name>`
  - `wd-admin list`
  - `wd-admin revoke <discord_name>`
  - `wd-admin sync`
  - `mc-admin-sync`
