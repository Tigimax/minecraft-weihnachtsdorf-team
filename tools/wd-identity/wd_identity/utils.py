"""
Utility and validation functions for wd-identity.
Handles input sanitization, Minecraft IGN validation, and SQL/LDAP safety.
"""

import re
import unicodedata
from typing import Optional


UMLAUT_MAP = {
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
    "Ä": "ae",
    "Ö": "oe",
    "Ü": "ue",
}


def sanitize_username(raw: str) -> str:
    """
    Sanitizes raw username (from Discord, CLI, or web) into a valid LDAP UID.
    - Transliterates German umlauts (ä->ae, etc.)
    - Removes accents and non-ASCII characters
    - Normalizes separators (#, spaces, dots) to underscores
    - Restricts to [a-z0-9._-]
    """
    if not raw:
        return "user"

    text = raw.strip()
    for char, replacement in UMLAUT_MAP.items():
        text = text.replace(char, replacement)

    # Decompose unicode accents
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")

    # Lowercase and replace non-alphanumeric symbols
    cleaned = ascii_text.lower()
    cleaned = re.sub(r"[\s#:/\\,@+]+", "_", cleaned)
    cleaned = re.sub(r"[^a-z0-9._-]", "", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")

    if not cleaned:
        return "user"
    return cleaned


def sanitize_email(username: str, domain: str = "tigimax.mooo.com", raw_email: Optional[str] = None) -> str:
    """
    Returns a valid ASCII email address conforming to LDAP IA5String syntax.
    If raw_email is provided, validates that it is pure ASCII; otherwise falls back to {clean_user}@{domain}.
    """
    clean_user = sanitize_username(username)
    if raw_email:
        raw_email = raw_email.strip().lower()
        # Check if pure ASCII and valid format
        if re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", raw_email):
            try:
                raw_email.encode("ascii")
                return raw_email
            except UnicodeEncodeError:
                pass
    return f"{clean_user}@{domain}"


def validate_minecraft_ign(ign: str) -> bool:
    """
    Validates Minecraft In-Game Name.
    Strictly permits only 1-16 alphanumeric characters and underscores.
    Rejects spaces, newlines, semicolons, and special characters.
    """
    if not ign or not isinstance(ign, str):
        return False
    return bool(re.match(r"^[a-zA-Z0-9_]{1,16}$", ign.strip()))


def escape_sql_literal(val: str) -> str:
    """
    Safely escapes single quotes for SQL string literals.
    """
    if val is None:
        return ""
    return str(val).replace("'", "''")
