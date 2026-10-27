"""
Mojang API client for resolving Minecraft player names and UUIDs.
"""

import hashlib
import logging
from typing import Optional, Tuple
import uuid
import requests

logger = logging.getLogger(__name__)


def format_uuid(raw_id: str) -> str:
    """Formats 32-char hex string into 8-4-4-4-12 UUID format."""
    clean = raw_id.replace("-", "").strip()
    if len(clean) != 32:
        return raw_id
    return f"{clean[0:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:32]}"


def offline_player_uuid(username: str) -> str:
    """Generates standard offline-mode UUID (Java UUID.nameUUIDFromBytes)."""
    # Equivalent to UUID.nameUUIDFromBytes(("OfflinePlayer:" + name).getBytes(StandardCharsets.UTF_8))
    # which uses MD5 hashing (UUID version 3)
    data = f"OfflinePlayer:{username}".encode("utf-8")
    md5_hash = bytearray(hashlib.md5(data).digest())
    # Set version to 3
    md5_hash[6] = (md5_hash[6] & 0x0F) | 0x30
    # Set variant to IETF
    md5_hash[8] = (md5_hash[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(md5_hash)))


def get_player_profile(ign: str) -> Tuple[str, str]:
    """
    Resolves canonical Minecraft name and UUID.
    Returns (canonical_name, formatted_uuid).
    Falls back to offline UUID if Mojang API is unavailable or user does not exist.
    """
    clean_ign = ign.strip()
    url = f"https://api.mojang.com/users/profiles/minecraft/{clean_ign}"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            canonical_name = data.get("name", clean_ign)
            raw_uuid = data.get("id", "")
            return canonical_name, format_uuid(raw_uuid)
    except Exception as e:
        logger.warning(f"Mojang API lookup failed for {clean_ign}: {e}")

    # Fallback to offline UUID
    return clean_ign, offline_player_uuid(clean_ign)
