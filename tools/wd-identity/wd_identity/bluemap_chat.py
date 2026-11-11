"""
BlueMap Web Chat and Authentication Integration Router for Weihnachtsdorf 7.
Enables real-time bi-directional chat between BlueMap web users and the Minecraft server,
enforcing authentic Minecraft usernames derived securely from Authentik SSO sessions.
"""

import asyncio
import ipaddress
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from wd_identity.config import settings
from wd_identity.ldap_client import LDAPClient
from wd_identity.mc_sync import MinecraftSyncClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bluemap-chat"])

# Internal registry of active SSE subscriber queues
_active_sse_queues: List[asyncio.Queue] = []
PROTECTED_INTERNAL_ACCOUNTS = {"tigimax", "akadmin"}
_shutdown_event: Optional[asyncio.Event] = None


def get_shutdown_event() -> asyncio.Event:
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    return _shutdown_event


def notify_shutdown() -> None:
    """Signals all SSE streams to terminate gracefully."""
    evt = get_shutdown_event()
    evt.set()
    for q in list(_active_sse_queues):
        try:
            q.put_nowait({"type": "shutdown"})
        except Exception:
            pass


def reset_shutdown_state() -> None:
    """Resets shutdown event for unit test isolation."""
    global _shutdown_event
    _shutdown_event = None


def client_ip_allowed(request: Request, allowed_csv: str) -> bool:
    """
    Validates if the direct client IP of the request is within the allowed CSV of IPs or CIDR networks.
    Empty or whitespace allowed_csv disables filtering (returns True).
    Evaluates only direct connection IP (request.client), never X-Forwarded-For.
    """
    if not allowed_csv or not allowed_csv.strip():
        return True

    client = getattr(request, "client", None)
    if not client:
        return False
    host = getattr(client, "host", None)
    if not host:
        return False

    try:
        client_ip = ipaddress.ip_address(host)
    except ValueError:
        return False

    for item in allowed_csv.split(","):
        raw_net = item.strip()
        if not raw_net:
            continue
        try:
            net = ipaddress.ip_network(raw_net, strict=False)
            if client_ip in net:
                return True
        except (ValueError, TypeError):
            continue

    return False


def resolve_minecraft_player_from_authentik(authentik_user: str) -> Tuple[str, str]:
    """
    Resolves Authentik username (e.g. 'tigimax_player', 'tubuu') to verified
    Minecraft Ingame Name (e.g. 'Tigimax', 'GoodTimesWithTub') and Mojang UUID.
    Guarantees web chat messages strictly display the user's authentic Minecraft account.
    """
    if not authentik_user:
        return ("", "")

    clean_user = authentik_user.strip().lower()
    ldap = LDAPClient()
    user = None

    if clean_user in PROTECTED_INTERNAL_ACCOUNTS:
        user = ldap.get_user(f"{clean_user}_player")
    if not user:
        user = ldap.get_user(clean_user)
    if not user:
        user = ldap.search_by_discord(clean_user)

    if not user:
        return (authentik_user, "")

    ign = user.get("minecraftName") or user.get("uid") or authentik_user
    uuid = user.get("minecraftUuid") or ""
    return (ign, uuid)


async def broadcast_bluemap_event(event_dict: Dict[str, Any]) -> None:
    """Dispatches a chat, join, quit, or death event to all connected BlueMap web clients."""
    dead_queues = []
    for q in _active_sse_queues:
        try:
            q.put_nowait(event_dict)
        except asyncio.QueueFull:
            dead_queues.append(q)
    for dq in dead_queues:
        if dq in _active_sse_queues:
            _active_sse_queues.remove(dq)


@router.get("/addons/integration/whoami")
async def whoami(request: Request):
    """
    BlueMap Integration endpoint.
    Returns the verified Minecraft username and UUID for the currently authenticated session.
    """
    if not client_ip_allowed(request, settings.PROXY_TRUSTED_IPS):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(f"Forbidden access to /addons/integration/whoami from {client_ip}")
        raise HTTPException(status_code=403, detail="Forbidden")

    auth_user = request.headers.get("x-authentik-username", "")
    if not auth_user:
        return {}

    ign, uuid = resolve_minecraft_player_from_authentik(auth_user)
    if not ign:
        return {}

    return {"uuid": uuid, "username": ign}


@router.get("/addons/integration/playerhead/{map_id}")
async def playerhead(map_id: str, request: Request):
    """Redirects to the 32x32 playerhead avatar for the logged-in BlueMap user."""
    if not client_ip_allowed(request, settings.PROXY_TRUSTED_IPS):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(f"Forbidden access to /addons/integration/playerhead from {client_ip}")
        raise HTTPException(status_code=403, detail="Forbidden")

    auth_user = request.headers.get("x-authentik-username", "")
    _, uuid = resolve_minecraft_player_from_authentik(auth_user)
    clean_uuid = uuid.replace("-", "") if uuid else "steve"
    return RedirectResponse(
        url=f"https://mc-heads.net/avatar/{clean_uuid}/32",
        status_code=302,
    )


class ChatSendMessageRequest(BaseModel):
    message: str


@router.post("/addons/chat/{map_id}/send")
async def send_web_chat(map_id: str, payload: ChatSendMessageRequest, request: Request):
    """
    Receives web chat messages from BlueMap, broadcasts to Minecraft via RCON,
    and echoes to all active BlueMap SSE listeners with verified Minecraft identity.
    """
    if not client_ip_allowed(request, settings.PROXY_TRUSTED_IPS):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(f"Forbidden web chat send attempt from {client_ip}")
        raise HTTPException(status_code=403, detail="Forbidden")

    auth_user = request.headers.get("x-authentik-username", "")
    if not auth_user:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt. Bitte über Authentik anmelden.")

    clean_msg = payload.message.strip()
    if not clean_msg:
        raise HTTPException(status_code=400, detail="Nachricht darf nicht leer sein.")
    if len(clean_msg) > 256:
        clean_msg = clean_msg[:256]

    ign, uuid = resolve_minecraft_player_from_authentik(auth_user)
    if not ign:
        ign = auth_user

    logger.info(f"[WebChat] Message from {ign} ({auth_user}): {clean_msg}")

    # 1. Broadcast to in-game Minecraft players with formatted prefix & gold username
    mc = MinecraftSyncClient()
    tellraw_payload = json.dumps([
        {"text": "[Web] ", "color": "aqua", "bold": True},
        {"text": f"<{ign}> ", "color": "gold"},
        {"text": clean_msg, "color": "white"}
    ])
    try:
        await asyncio.to_thread(mc.execute_commands, [f"execute in minecraft:overworld run tellraw @a {tellraw_payload}"])
    except Exception as e:
        logger.warning(f"Failed to broadcast webchat to Minecraft: {e}")

    # 2. Broadcast to all active BlueMap SSE sessions
    event_payload = {
        "type": "webchat",
        "uuid": uuid or "0",
        "username": ign,
        "message": clean_msg,
    }
    await broadcast_bluemap_event(event_payload)

    # 3. Enqueue to Discord bridge
    try:
        from wd_identity import chat_bridge
        await chat_bridge.enqueue_to_discord({
            "type": "webchat",
            "uuid": uuid or "0",
            "username": ign,
            "message": clean_msg,
        })
    except Exception as e:
        logger.warning(f"Failed to enqueue webchat to discord: {e}")

    return {"ok": True}


@router.get("/addons/chat/{map_id}/stream")
async def chat_stream(map_id: str, request: Request):
    """
    Server-Sent Events (SSE) stream for BlueMap web chat.
    Pushes live in-game chat, web chat, join, leave, and death messages.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _active_sse_queues.append(queue)

    async def sse_generator():
        evt = get_shutdown_event()
        # Handshake: notify frontend that chat is enabled and read-write
        init_data = json.dumps({"type": "settings", "readOnly": False})
        yield f"data: {init_data}\n\n"
        try:
            while not evt.is_set():
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=2.0)
                    if evt.is_set() or event.get("type") == "shutdown":
                        break
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    if evt.is_set():
                        break
                    yield ": ping\n\n"
        finally:
            if queue in _active_sse_queues:
                _active_sse_queues.remove(queue)

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class IngameChatEvent(BaseModel):
    type: str = "chat"
    uuid: Optional[str] = "0"
    username: Optional[str] = ""
    message: Optional[str] = ""
    source: Optional[str] = ""


@router.post("/api/bluemap/chat/ingame")
async def ingame_chat_event(event: IngameChatEvent, request: Request):
    """
    Internal bridge receiver called by Minecraft server (BlueMapLiveBridge)
    to forward in-game chat, join, leave, and death events to BlueMap web chat.
    """
    if not client_ip_allowed(request, settings.BRIDGE_TRUSTED_IPS):
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(f"Forbidden ingame chat event from {client_ip}")
        raise HTTPException(status_code=403, detail="Forbidden")

    payload = event.model_dump() if hasattr(event, "model_dump") else event.dict()
    await broadcast_bluemap_event(payload)

    if event.source != "discord":
        try:
            from wd_identity import chat_bridge
            await chat_bridge.enqueue_to_discord(payload)
        except Exception as e:
            logger.warning(f"Failed to enqueue ingame event to discord: {e}")

    return {"ok": True}
