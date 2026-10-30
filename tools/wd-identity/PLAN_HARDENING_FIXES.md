# Implementation Plan: Identity Hardening & Security Fixes

## Problem Analysis & Deficiencies in Prior Attempt

1. **Role Escalation via Spoofed Discord ID / Name in Webhook**:
   - `webhook.process_onboarding` accepted `req.discord_id` and `req.discord_name` directly when `trusted_discord_id` was not yet set.
   - For any new registration, an attacker providing `role="admin"` and the Discord ID or name of an administrator (e.g. `Tigimax`) had `check_is_discord_admin` run against that ID/name, which returned `True`, granting the attacker full LDAP admin, Authentik `wd_admins`, Planka admin, and Minecraft OP.
   - The same escalation applied to `role="mod"`.

2. **MFA Token Redirection on Unlinked LDAP Accounts**:
   - `webhook.mfa_auto_dispatch` fell back to `req.discord_id` whenever an existing LDAP user had no `discordId` bound.
   - An attacker could supply `username="victim"` and `discord_id="<attacker_id>"` and receive the victim's 6-digit MFA one-time token in their own Discord DM.

3. **Discord ID Hijacking**:
   - An onboarding request could bind a `discord_id` that is already bound to another LDAP user.

4. **Production Code Test Pollution**:
   - `discord_bot.py` contained `if type(user).__name__ == "MagicMock":` in three critical functions (`handle_auth_code_request`, `cmd_password_reset`, `cmd_status`).

5. **Mutable Identity Lookups in Discord Bot**:
   - `handle_admin_appeal_accept_submit` fell back to `ldap.search_by_discord(discord_name)` instead of strict `discord_id`.
   - `cmd_spieler_info` fell back to `discord_nutzer.name` and `display_name`.

6. **Planka PostgreSQL DO Block Delimiter Safety**:
   - PostgreSQL `DO $$ ... $$` blocks did not escape or strip `$` from user-provided `name`, allowing block escape or syntax errors.

7. **Missing Authentik Group Cleanup Integration**:
   - `webhook.py` inlined loops instead of using `ak.sync_user_groups`.
   - `membership.py` did not remove Authentik groups during privilege revocation.

---

## Detailed Step-by-Step Changes

### Step 1: Tests First
- Added 6 failing tests in `tests/test_security_hardening.py` covering:
  - New user admin escalation prevention with spoofed `discord_id`.
  - New user admin escalation prevention with spoofed `discord_name`.
  - New user mod escalation prevention with spoofed `discord_id`.
  - MFA auto-dispatch rejection on existing unlinked users.
  - Prevention of binding a `discord_id` already owned by another user.
  - Elimination of `MagicMock` checks in production code.

### Step 2: Webhook Hardening (`wd_identity/webhook.py`)
- In `verify_webhook_auth`: Log prominent security warning if `WEBHOOK_SECRET` is unset.
- In `mfa_auto_dispatch`:
  - If `user` exists in LDAP, `bound_discord_id` MUST be present. If empty, return error and never fall back to `req.discord_id`.
  - Only allow `req.discord_id` during initial pre-LDAP enrollment.
- In `process_onboarding`:
  - Check whether `req.discord_id` is already bound to a different user in LDAP; clear it if taken.
  - Enforce that elevated roles (`admin`, `mod`) can ONLY be granted if authorized by a trusted source (`existing.get("role")` or `ak_attrs.get("role")`).
  - Require a valid `trusted_discord_id` for admin verification; verify strictly using immutable `trusted_discord_id`.
  - Replace manual group removal loops with `ak.sync_user_groups`.

### Step 3: Discord Bot Hardening (`wd_identity/discord_bot.py` & `tests/test_mfa_bot.py`)
- Remove all `MagicMock` type checks from `discord_bot.py`.
- Call `ldap.search_by_discord_id(discord_id)` directly.
- In `test_mfa_bot.py`, mock `mock_ldap.search_by_discord_id` properly.
- In `handle_admin_appeal_accept_submit`, search strictly by `discord_id`.
- In `cmd_spieler_info`, lookup strictly by `discord_id`.

### Step 4: Planka & Membership Hardening
- In `planka_sync.py`: Strip `$` from `raw_name` and use named dollar delimiter `DO $planka_block$ ... END $planka_block$;`.
- In `membership.py`: Ensure `revoke_user_privileges` strips Authentik groups (`wd_admins`, `wd_mods`, `wd_players`), and `restore_user_privileges` synchronizes them via `authentik.sync_user_groups`.
- In `utils.py`: Restrict `check_is_discord_admin` and `check_is_discord_mod` to strict UID match when numeric target is provided.

### Step 5: Verification
- Re-run all tests in `tests/test_security_hardening.py`.
- Run full pytest suite across all modules.
- Verify zero regressions.
