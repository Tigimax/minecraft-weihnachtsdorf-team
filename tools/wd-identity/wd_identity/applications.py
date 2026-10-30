"""
Application and ban state management for Weihnachtsdorf 7.
Manages candidate applications, unban appeals, cooldowns, and server bans using SQLite.
"""

from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional

from wd_identity.config import settings

logger = logging.getLogger(__name__)


def get_db_path() -> Path:
    data_dir = getattr(settings, "DATA_DIR", Path("/opt/wd-identity/data"))
    if not data_dir.exists():
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Fallback to local ./data or /tmp if directory creation failed
            data_dir = Path("./data")
            data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "applications.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initializes tables for applications and bans."""
    conn = get_connection()
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                discord_username TEXT NOT NULL,
                app_type TEXT NOT NULL, -- 'initial' or 'unban'
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', -- 'pending', 'accepted', 'rejected'
                reviewer_id TEXT,
                reviewer_name TEXT,
                reason TEXT,
                cooldown_until TIMESTAMP,
                channel_msg_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT,
                minecraft_ign TEXT,
                banned_by TEXT,
                reason TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_apps_discord ON applications (discord_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_apps_status ON applications (status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bans_discord ON bans (discord_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bans_ign ON bans (minecraft_ign);")
    conn.close()


def create_application(
    discord_id: str,
    discord_username: str,
    app_type: str,
    message: str,
) -> int:
    """Creates a new pending application."""
    init_db()
    conn = get_connection()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO applications (discord_id, discord_username, app_type, message, status, updated_at)
            VALUES (?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)
            """,
            (str(discord_id), discord_username, app_type, message),
        )
        app_id = cursor.lastrowid
    conn.close()
    return app_id


def set_channel_message_id(app_id: int, channel_msg_id: str):
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE applications SET channel_msg_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (str(channel_msg_id), app_id),
        )
    conn.close()


def get_application(app_id: Optional[int] = None, discord_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    if app_id is not None:
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    elif discord_id is not None:
        row = conn.execute("SELECT * FROM applications WHERE discord_id = ? ORDER BY id DESC LIMIT 1", (str(discord_id),)).fetchone()
    else:
        row = None
    conn.close()
    return dict(row) if row else None


def get_user_status(discord_id: str) -> Dict[str, Any]:
    """
    Checks the status of a user:
    - has_pending: bool (if there's an unresolved request)
    - pending_app: dict | None
    - is_rejected: bool (latest initial application was rejected)
    - is_banned: bool (has active ban record)
    - active_ban: dict | None
    - in_cooldown: bool (cannot submit appeal yet)
    - is_permanent_cooldown: bool
    - cooldown_until: datetime | None
    - cooldown_str: str | None
    """
    init_db()
    conn = get_connection()
    discord_str = str(discord_id)

    # 1. Check active ban
    ban_row = conn.execute(
        "SELECT * FROM bans WHERE discord_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
        (discord_str,),
    ).fetchone()
    active_ban = dict(ban_row) if ban_row else None

    # 2. Check pending applications
    pending_row = conn.execute(
        "SELECT * FROM applications WHERE discord_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (discord_str,),
    ).fetchone()
    pending_app = dict(pending_row) if pending_row else None

    # 3. Check latest application
    latest_row = conn.execute(
        "SELECT * FROM applications WHERE discord_id = ? ORDER BY id DESC LIMIT 1",
        (discord_str,),
    ).fetchone()
    latest_app = dict(latest_row) if latest_row else None

    # 4. Check active cooldown across all applications
    cooldown_row = conn.execute(
        """
        SELECT cooldown_until FROM applications
        WHERE discord_id = ? AND cooldown_until IS NOT NULL
        ORDER BY cooldown_until DESC LIMIT 1
        """,
        (discord_str,),
    ).fetchone()

    in_cooldown = False
    is_permanent = False
    cooldown_dt = None
    cooldown_str = None

    if cooldown_row and cooldown_row["cooldown_until"]:
        raw_cd = cooldown_row["cooldown_until"]
        try:
            if isinstance(raw_cd, str):
                # SQLite TIMESTAMP parse
                cd_clean = raw_cd.replace("T", " ")
                if "." in cd_clean:
                    cd_clean = cd_clean.split(".")[0]
                cooldown_dt = datetime.strptime(cd_clean, "%Y-%m-%d %H:%M:%S")
            else:
                cooldown_dt = raw_cd

            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            if cooldown_dt.year >= 2090:
                in_cooldown = True
                is_permanent = True
                cooldown_str = "Dauerhaft (Permanent)"
            elif cooldown_dt > now_utc:
                in_cooldown = True
                remaining = cooldown_dt - now_utc
                days = remaining.days
                hours = remaining.seconds // 3600
                minutes = (remaining.seconds % 3600) // 60
                date_fmt = cooldown_dt.strftime("%d.%m.%Y um %H:%M Uhr UTC")
                if days > 0:
                    cooldown_str = f"noch {days} Tag(e) und {hours} Stunde(n) (bis {date_fmt})"
                elif hours > 0:
                    cooldown_str = f"noch {hours} Stunde(n) und {minutes} Minute(n) (bis {date_fmt})"
                else:
                    cooldown_str = f"noch {minutes} Minute(n) (bis {date_fmt})"
        except Exception as e:
            logger.warning(f"Failed to parse cooldown timestamp '{raw_cd}': {e}")

    conn.close()

    is_rejected = bool(latest_app and latest_app["status"] == "rejected")

    return {
        "discord_id": discord_str,
        "has_pending": pending_app is not None,
        "pending_app": pending_app,
        "is_rejected": is_rejected,
        "latest_app": latest_app,
        "is_banned": active_ban is not None,
        "active_ban": active_ban,
        "in_cooldown": in_cooldown,
        "is_permanent_cooldown": is_permanent,
        "cooldown_until": cooldown_dt,
        "cooldown_str": cooldown_str,
    }


def accept_application(app_id: int, reviewer_id: str, reviewer_name: str) -> Optional[Dict[str, Any]]:
    """Marks application as accepted and clears any active bans."""
    init_db()
    conn = get_connection()
    with conn:
        conn.execute(
            """
            UPDATE applications
            SET status = 'accepted', reviewer_id = ?, reviewer_name = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(reviewer_id), reviewer_name, app_id),
        )
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        if row:
            # Lift active bans for this discord_id
            conn.execute(
                "UPDATE bans SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE discord_id = ?",
                (row["discord_id"],),
            )
    conn.close()
    return dict(row) if row else None


def reject_application(
    app_id: int,
    reviewer_id: str,
    reviewer_name: str,
    reason: Optional[str] = None,
    cooldown_days: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Marks application as rejected with an optional cooldown period."""
    init_db()
    clean_reason = (reason or "").strip()
    if not clean_reason:
        clean_reason = "Vielen Dank für dein Interesse. Leider können wir deine Bewerbung zum aktuellen Zeitpunkt nicht berücksichtigen."

    cooldown_until = None
    if cooldown_days is not None:
        if cooldown_days < 0:
            # Permanent
            cooldown_until = "2099-12-31 23:59:59"
        else:
            dt = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=cooldown_days)
            cooldown_until = dt.strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    with conn:
        conn.execute(
            """
            UPDATE applications
            SET status = 'rejected', reviewer_id = ?, reviewer_name = ?, reason = ?, cooldown_until = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(reviewer_id), reviewer_name, clean_reason, cooldown_until, app_id),
        )
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def record_ban(
    discord_id: Optional[str],
    minecraft_ign: Optional[str],
    banned_by: str,
    reason: Optional[str] = None,
) -> int:
    """Records an active player ban."""
    init_db()
    conn = get_connection()
    clean_reason = (reason or "").strip() or "Verstoß gegen die Server-Regeln."
    with conn:
        # Mark prior bans for this discord_id or ign as inactive
        if discord_id:
            conn.execute("UPDATE bans SET is_active = 0 WHERE discord_id = ?", (str(discord_id),))
        if minecraft_ign:
            conn.execute("UPDATE bans SET is_active = 0 WHERE LOWER(minecraft_ign) = LOWER(?)", (minecraft_ign.strip(),))

        cursor = conn.execute(
            """
            INSERT INTO bans (discord_id, minecraft_ign, banned_by, reason, is_active, updated_at)
            VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
            (str(discord_id) if discord_id else None, minecraft_ign, banned_by, clean_reason),
        )
        ban_id = cursor.lastrowid
    conn.close()
    return ban_id


def lift_ban(discord_id: Optional[str] = None, minecraft_ign: Optional[str] = None) -> int:
    """Lifts any active ban for given discord_id or minecraft_ign."""
    init_db()
    conn = get_connection()
    count = 0
    with conn:
        if discord_id:
            c = conn.execute("UPDATE bans SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE discord_id = ? AND is_active = 1", (str(discord_id),))
            count += c.rowcount
        if minecraft_ign:
            c = conn.execute("UPDATE bans SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE LOWER(minecraft_ign) = LOWER(?) AND is_active = 1", (minecraft_ign.strip(),))
            count += c.rowcount
    conn.close()
    return count


def purge_user_history(discord_id: Optional[str] = None, minecraft_ign: Optional[str] = None) -> int:
    """Completely purges all applications, review history, bans, and cooldowns for a user."""
    init_db()
    conn = get_connection()
    count = 0
    with conn:
        if discord_id:
            c1 = conn.execute("DELETE FROM applications WHERE discord_id = ?", (str(discord_id),)).rowcount
            c2 = conn.execute("DELETE FROM bans WHERE discord_id = ?", (str(discord_id),)).rowcount
            count += c1 + c2
        if minecraft_ign:
            c3 = conn.execute("DELETE FROM bans WHERE LOWER(minecraft_ign) = LOWER(?)", (minecraft_ign.strip(),)).rowcount
            count += c3
    conn.close()
    return count


def list_applications(
    status: Optional[str] = None,
    app_type: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Lists applications with optional filtering."""
    init_db()
    conn = get_connection()
    query = "SELECT * FROM applications WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if app_type:
        query += " AND app_type = ?"
        params.append(app_type)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]
