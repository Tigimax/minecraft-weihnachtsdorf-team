# Security & Repository Guidelines for Antigravity

## 🔒 Confidentiality & Public Repository Rules

### 1. Dual Repository Setup
* **`origin` (`tigimax/minecraft-weihnachtsdorf`)**: Private development repository.
* **`team` (`Tigimax/minecraft-weihnachtsdorf-team`)**: **PUBLIC** repository visible to the entire team and community.

### 2. STRICT PROHIBITION: No Secrets in Code, Commits or History
* **NEVER** write, commit, or push real secrets, tokens, or passwords to this codebase:
  * LDAP bind credentials (`LDAP_BIND_PASSWORD`)
  * Authentik admin tokens (`AUTHENTIK_TOKEN`)
  * Pterodactyl Wings bearer tokens (`WINGS_BEARER_TOKEN`)
  * Database passwords (MariaDB, PostgreSQL)
  * Minecraft Velocity forwarding secrets (`forwarding.secret`)
  * RCON passwords or management secrets
  * Discord Bot tokens
* **NEVER** commit private homelab network infrastructure, non-project hostnames, or personal client IPs (e.g., personal workstations, TV, storage/media servers, root Proxmox hypervisor nodes).
* **Only use official placeholders**:
  * `YOUR_LDAP_BIND_PASSWORD`
  * `YOUR_AUTHENTIK_API_TOKEN`
  * `YOUR_WINGS_BEARER_TOKEN`
  * `CHANGE_ME_PTERO_DB_PASSWORD`
  * `<FORWARDING_SECRET>`
* In Python/scripts, fallback defaults for secrets MUST always be empty strings (e.g., `os.getenv("WINGS_BEARER_TOKEN", "")`), NEVER real tokens.

### 3. Production Credentials Isolation
* Production secrets live **exclusively** on the production server in `/etc/default/wd-identity` or local `.env` files.
* `/etc/default/wd-identity` and `tools/wd-identity/etc/default/wd-identity` MUST remain in `.gitignore`.
* Only template files with the `.example` suffix (e.g. `wd-identity.example`) may be committed.

### 4. Verification Before Any Git Push
* Before running any `git push` (especially to `team`):
  1. Inspect `git status` and `git diff` to ensure no sensitive files are tracked.
  2. Verify that no commit message reveals historical secret removals or compromises.
  3. Ensure that only project-relevant services and hostnames are included in `custom.list` and Ansible playbooks.
