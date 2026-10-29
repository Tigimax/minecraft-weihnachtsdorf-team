"""
Central Audit and Security Logger for Weihnachtsdorf 7.
Dispatches formatted log embeds to the #admin-logs Discord channel.
Synchronous and REST-based so it can be called reliably from Bot, Webhook, and CLI.
"""

from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

from wd_identity.config import settings

logger = logging.getLogger(__name__)

COLOR_INFO = 0x3498db       # Blue
COLOR_SUCCESS = 0x2ecc71    # Green
COLOR_WARNING = 0xf39c12    # Orange / Yellow
COLOR_ERROR = 0xe74c3c      # Red
COLOR_PURPLE = 0x9b59b6     # Purple (Admin actions)


def log_admin_event(
    title: str,
    description: str,
    color: int = COLOR_INFO,
    fields: Optional[List[Dict[str, Any]]] = None,
    author_name: Optional[str] = None,
    footer_text: str = "Weihnachtsdorf 7 Audit Log",
) -> bool:
    """
    Sends an audit event embed to the #admin-logs channel.
    Operates synchronously via Discord REST API.
    """
    token = settings.DISCORD_BOT_TOKEN.strip() if settings.DISCORD_BOT_TOKEN else ""
    channel_id = getattr(settings, "DISCORD_CHANNEL_LOGS_ID", "").strip()

    if not token or not channel_id:
        logger.debug(f"[Audit Log Mock] {title} - {description}")
        return False

    embed: Dict[str, Any] = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {
            "text": footer_text,
        },
    }

    if author_name:
        embed["author"] = {"name": author_name}

    if fields:
        valid_fields = []
        for f in fields:
            name = str(f.get("name", "Info"))[:256]
            value = str(f.get("value", "-"))[:1024]
            inline = bool(f.get("inline", False))
            valid_fields.append({"name": name, "value": value, "inline": inline})
        embed["fields"] = valid_fields

    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordBot-WD7 (https://tigimax.mooo.com)",
        "Content-Type": "application/json",
    }

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    payload = json.dumps({"embeds": [embed]}).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status in (200, 201)
    except Exception as e:
        logger.warning(f"Failed to post to #admin-logs: {e}")
        return False
