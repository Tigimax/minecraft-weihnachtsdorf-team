"""
Mojang API client for resolving Minecraft player names and UUIDs.
"""

from functools import lru_cache
import hashlib
import logging
from typing import Optional, Tuple
import uuid
import requests

from wd_identity.utils import validate_minecraft_ign

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


@lru_cache(maxsize=256)
def get_player_profile(ign: str, require_existing: bool = False) -> Tuple[str, str]:
    """
    Resolves canonical Minecraft name and UUID.
    Returns (canonical_name, formatted_uuid).
    Validates IGN format, uses LRU cache to prevent Mojang rate-limiting.
    When require_existing=True, raises ValueError if account does not exist at Mojang.
    When require_existing=False, falls back to offline UUID if Mojang API is unavailable or user does not exist.
    """
    clean_ign = (ign or "").strip()
    if not validate_minecraft_ign(clean_ign):
        logger.warning(f"Invalid Minecraft IGN rejected: '{clean_ign}'")
        raise ValueError(f"Invalid Minecraft IGN: '{clean_ign}' (must be 1-16 alphanumeric characters or underscores)")

    url = f"https://api.mojang.com/users/profiles/minecraft/{clean_ign}"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if not data or not data.get("id"):
                if require_existing:
                    raise ValueError(f"Minecraft-Account '{clean_ign}' existiert nicht bei Mojang.")
            else:
                canonical_name = data.get("name", clean_ign)
                raw_uuid = data.get("id", "")
                return canonical_name, format_uuid(raw_uuid)
        elif res.status_code in (204, 404):
            if require_existing:
                raise ValueError(f"Minecraft-Account '{clean_ign}' existiert nicht bei Mojang.")
            logger.info(f"Mojang profile not found ({res.status_code}) for '{clean_ign}', falling back to offline UUID.")
        elif res.status_code == 429:
            logger.warning(f"Mojang API rate limited (429) for '{clean_ign}'; falling back to offline UUID.")
            if require_existing:
                raise RuntimeError(f"Mojang API Rate Limit erreicht (429) für '{clean_ign}'. Bitte versuche es später erneut.")
        else:
            logger.warning(f"Mojang API returned status {res.status_code} for '{clean_ign}'")
            if require_existing:
                raise RuntimeError(f"Mojang API Serverfehler (HTTP {res.status_code}) für '{clean_ign}'.")
    except (ValueError, RuntimeError):
        raise
    except requests.RequestException as e:
        logger.warning(f"Mojang API request exception for {clean_ign}: {e}")
        if require_existing:
            raise
    except Exception as e:
        logger.warning(f"Mojang API lookup failed for {clean_ign}: {e}")
        if require_existing:
            raise RuntimeError(f"Mojang API Fehler: {e}") from e

    # Fallback to offline UUID
    return clean_ign, offline_player_uuid(clean_ign)
