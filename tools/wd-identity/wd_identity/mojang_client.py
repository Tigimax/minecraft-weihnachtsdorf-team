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


class _MojangLookupError(Exception):
    """Internal exception raised when Mojang API response should not be cached."""
    pass


class _MojangNotFoundError(_MojangLookupError):
    pass


class _MojangRateLimitError(_MojangLookupError):
    pass


class _MojangServerError(_MojangLookupError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Mojang server error: {status_code}")


@lru_cache(maxsize=256)
def _fetch_mojang_online(lookup_ign: str) -> Tuple[str, str]:
    """
    Fetches canonical name and UUID from Mojang API.
    ONLY successful HTTP 200 responses are returned and cached.
    Any non-200, 404, 429, 5xx, or network error raises an internal exception
    which avoids poisoning the LRU cache with temporary or offline fallback data.
    """
    url = f"https://api.mojang.com/users/profiles/minecraft/{lookup_ign}"
    try:
        res = requests.get(url, timeout=10)
    except requests.RequestException as e:
        raise _MojangLookupError(f"Mojang API request exception: {e}") from e
    except Exception as e:
        raise _MojangLookupError(f"Mojang API lookup failed: {e}") from e

    if res.status_code == 200:
        data = res.json()
        if not data or not data.get("id"):
            raise _MojangNotFoundError(f"Minecraft-Account '{lookup_ign}' existiert nicht bei Mojang.")
        canonical_name = data.get("name", lookup_ign)
        raw_uuid = data.get("id", "")
        return canonical_name, format_uuid(raw_uuid)
    elif res.status_code in (204, 404):
        raise _MojangNotFoundError(f"Minecraft-Account '{lookup_ign}' existiert nicht bei Mojang.")
    elif res.status_code == 429:
        raise _MojangRateLimitError(f"Mojang API Rate Limit erreicht (429) für '{lookup_ign}'.")
    else:
        raise _MojangServerError(res.status_code)


def get_player_profile(ign: str, require_existing: bool = False) -> Tuple[str, str]:
    """
    Resolves canonical Minecraft name and UUID.
    Returns (canonical_name, formatted_uuid).
    Validates IGN format, uses LRU cache to prevent Mojang rate-limiting on valid profiles.
    When require_existing=True, raises ValueError if account does not exist at Mojang,
    or RuntimeError if API is unavailable.
    When require_existing=False, falls back to offline UUID without caching the fallback.
    """
    clean_ign = (ign or "").strip()
    if not validate_minecraft_ign(clean_ign):
        logger.warning(f"Invalid Minecraft IGN rejected: '{clean_ign}'")
        raise ValueError(f"Invalid Minecraft IGN: '{clean_ign}' (must be 1-16 alphanumeric characters or underscores)")

    try:
        return _fetch_mojang_online(clean_ign.lower())
    except _MojangNotFoundError:
        if require_existing:
            raise ValueError(f"Minecraft-Account '{clean_ign}' existiert nicht bei Mojang.")
        logger.info(f"Mojang profile not found for '{clean_ign}', falling back to offline UUID.")
    except _MojangRateLimitError:
        if require_existing:
            raise RuntimeError(f"Mojang API Rate Limit erreicht (429) für '{clean_ign}'. Bitte versuche es später erneut.")
        logger.warning(f"Mojang API rate limited (429) for '{clean_ign}'; falling back to offline UUID.")
    except _MojangServerError as e:
        if require_existing:
            raise RuntimeError(f"Mojang API Serverfehler (HTTP {e.status_code}) für '{clean_ign}'.")
        logger.warning(f"Mojang API returned status {e.status_code} for '{clean_ign}'")
    except _MojangLookupError as e:
        if require_existing:
            raise RuntimeError(f"Mojang API Fehler: {e}") from e
        logger.warning(f"Mojang API lookup failed for {clean_ign}: {e}")
    except Exception as e:
        if require_existing:
            raise RuntimeError(f"Mojang API Fehler: {e}") from e
        logger.warning(f"Mojang API lookup failed for {clean_ign}: {e}")

    # Fallback to offline UUID (NEVER cached by _fetch_mojang_online)
    return clean_ign, offline_player_uuid(clean_ign)


# Expose cache controls on get_player_profile for backwards compatibility and test isolation
get_player_profile.cache_clear = _fetch_mojang_online.cache_clear  # type: ignore[attr-defined]
get_player_profile.cache_info = _fetch_mojang_online.cache_info  # type: ignore[attr-defined]
