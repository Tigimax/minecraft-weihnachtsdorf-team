"""
Unit tests for Minecraft <-> Discord Chat & Console Bridge.
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import pytest

from wd_identity import chat_bridge
from wd_identity.config import settings
from wd_identity.discord_bot import bridge_on_message, cmd_mc_befehl, console_log_sync_loop
from wd_identity.mc_sync import new_log_lines


# ---------------------------------------------------------------------------
# Fixture to reset chat_bridge cache/limits across test runs
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_chat_bridge_state():
    chat_bridge._player_cache.clear()
    chat_bridge._minecraft_player_cache.clear()
    chat_bridge._avatar_cache.clear()
    chat_bridge._user_rate_limits.clear()
    chat_bridge._webhook_cache.clear()
    yield
    chat_bridge._player_cache.clear()
    chat_bridge._minecraft_player_cache.clear()
    chat_bridge._avatar_cache.clear()
    chat_bridge._user_rate_limits.clear()
    chat_bridge._webhook_cache.clear()
def test_clean_discord_text_mentions_and_emojis():
    msg = MagicMock()
    msg.clean_content = "Hello @Tigimax <:kek:123456789> and <a:party:987654321>!"
    msg.attachments = []
    msg.stickers = []
    result = chat_bridge.clean_discord_text(msg)
    assert result == "Hello @Tigimax :kek: and :party:!"


def test_clean_discord_text_newlines():
    msg = MagicMock()
    msg.clean_content = "First line\nSecond line\r\nThird line"
    msg.attachments = []
    msg.stickers = []
    result = chat_bridge.clean_discord_text(msg)
    assert result == "First line Second line Third line"


def test_clean_discord_text_strips_section_sign():
    msg = MagicMock()
    msg.clean_content = "§cRed §lBold §kObfuscated §rReset"
    msg.attachments = []
    msg.stickers = []
    result = chat_bridge.clean_discord_text(msg)
    assert "§" not in result
    assert result == "cRed lBold kObfuscated rReset"


def test_clean_discord_text_length_limit():
    msg = MagicMock()
    msg.clean_content = "A" * 300
    msg.attachments = []
    msg.stickers = []
    result = chat_bridge.clean_discord_text(msg)
    assert len(result) == 256
    assert result == "A" * 256


def test_clean_discord_text_with_attachments_and_stickers():
    # Only attachment, no text
    msg1 = MagicMock()
    msg1.clean_content = ""
    msg1.attachments = [MagicMock()]
    msg1.stickers = []
    assert chat_bridge.clean_discord_text(msg1) == "[Bild]"

    # Text + attachment
    msg2 = MagicMock()
    msg2.clean_content = "Schau dir das an"
    msg2.attachments = [MagicMock()]
    msg2.stickers = []
    assert chat_bridge.clean_discord_text(msg2) == "Schau dir das an [Bild]"

    # Only sticker
    msg3 = MagicMock()
    msg3.clean_content = ""
    msg3.attachments = []
    msg3.stickers = [MagicMock()]
    assert chat_bridge.clean_discord_text(msg3) == "[Bild]"


# ---------------------------------------------------------------------------
# build_tellraw tests
# ---------------------------------------------------------------------------
def test_build_tellraw_valid_json_escaping():
    ign = 'Tigi"max'
    text = 'He said "Hello" \\ and \u2764 with special \n newline'

    # 1. Admin role -> red
    cmd_admin = chat_bridge.build_tellraw(ign, text, role="admin")
    prefix = "execute in minecraft:overworld run tellraw @a "
    assert cmd_admin.startswith(prefix)
    data_admin = json.loads(cmd_admin[len(prefix):])

    assert isinstance(data_admin, list)
    assert len(data_admin) == 3

    # Element 1: Prefix [D]
    assert data_admin[0]["text"] == "[D] "
    assert data_admin[0]["color"] == "blue"
    assert data_admin[0]["bold"] is True
    assert data_admin[0]["hoverEvent"]["action"] == "show_text"
    assert data_admin[0]["hoverEvent"]["contents"] == "Discord"

    # Element 2: Ingame name (red for admin)
    assert data_admin[1]["text"] == f"<{ign}> "
    assert data_admin[1]["color"] == "red"

    # Element 3: Message text with Unicode & quotes preserved
    assert data_admin[2]["text"] == text
    assert data_admin[2]["color"] == "white"

    # 2. Mod role -> blue
    cmd_mod = chat_bridge.build_tellraw(ign, text, role="mod")
    data_mod = json.loads(cmd_mod[len(prefix):])
    assert data_mod[1]["color"] == "blue"

    # 3. Default / player role -> white
    cmd_player = chat_bridge.build_tellraw(ign, text, role="player")
    data_player = json.loads(cmd_player[len(prefix):])
    assert data_player[1]["color"] == "white"


# ---------------------------------------------------------------------------
# resolve_active_player tests
# ---------------------------------------------------------------------------
def test_resolve_active_player_linked_and_active():
    with patch("wd_identity.chat_bridge.LDAPClient") as mock_ldap_cls:
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord.return_value = {
            "uid": "testuser",
            "dn": "uid=testuser,ou=User,dc=ldap-01,dc=net",
            "minecraftName": "TestSteve",
            "minecraftUuid": "11112222-3333-4444-5555-666677778888",
        }
        mock_ldap.get_group_members.side_effect = lambda g: (
            ["uid=testuser,ou=User,dc=ldap-01,dc=net"] if "wd_players" in g else []
        )

        res = chat_bridge.resolve_active_player("123456789")
        assert res == ("TestSteve", "11112222-3333-4444-5555-666677778888")

        # Cache verification: LDAP should not be called again
        mock_ldap.search_by_discord.reset_mock()
        res_cached = chat_bridge.resolve_active_player("123456789")
        assert res_cached == ("TestSteve", "11112222-3333-4444-5555-666677778888")
        mock_ldap.search_by_discord.assert_not_called()


def test_resolve_active_player_not_in_allowed_group():
    with patch("wd_identity.chat_bridge.LDAPClient") as mock_ldap_cls:
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord.return_value = {
            "uid": "unprivileged_user",
            "dn": "uid=unprivileged_user,ou=User,dc=ldap-01,dc=net",
            "minecraftName": "Unprivileged",
            "minecraftUuid": "00000000-0000-0000-0000-000000000000",
        }
        mock_ldap.get_group_members.return_value = ["uid=someone_else,ou=User,dc=ldap-01,dc=net"]

        res = chat_bridge.resolve_active_player("999999999")
        assert res is None

        # Negative cache verification
        mock_ldap.search_by_discord.reset_mock()
        assert chat_bridge.resolve_active_player("999999999") is None
        mock_ldap.search_by_discord.assert_not_called()


def test_resolve_active_player_unlinked_discord():
    with patch("wd_identity.chat_bridge.LDAPClient") as mock_ldap_cls:
        mock_ldap = mock_ldap_cls.return_value
        mock_ldap.search_by_discord.return_value = None

        res = chat_bridge.resolve_active_player("unknown_id")
        assert res is None


# ---------------------------------------------------------------------------
# rate_ok tests
# ---------------------------------------------------------------------------
def test_rate_ok():
    user = "user_42"
    # First 5 calls must succeed
    for _ in range(5):
        assert chat_bridge.rate_ok(user) is True
    # 6th call within window must fail
    assert chat_bridge.rate_ok(user) is False

    # Empty user id fails
    assert chat_bridge.rate_ok("") is False


# ---------------------------------------------------------------------------
# clean_console_line & console_filter tests
# ---------------------------------------------------------------------------
def test_clean_console_line():
    # Exact user examples
    line1 = "[K[14:05:25 INFO]: [37m[[1m[37m[96mL[36mP[0m[37m][0m [36m[97m- [36mWeight: [97mNone[0m"
    assert chat_bridge.clean_console_line(line1) == "[14:05:25 INFO]: [LP] - Weight: None"

    line2 = ">....[?1h=[?2004h>...."
    assert chat_bridge.clean_console_line(line2) == ""

    line3 = "System chat: [91mNo player was found[0m"
    assert chat_bridge.clean_console_line(line3) == "System chat: No player was found"

    # Additional ANSI / VT100 / JLine tests
    line_esc = "\x1b[32mGreen\x1b[0m and \x1b[1;31mBold Red\x1b[0m\r\n"
    assert chat_bridge.clean_console_line(line_esc) == "Green and Bold Red"

    line_prompt = ">>>.... [12:00:00 INFO]: Hello world >....  "
    assert chat_bridge.clean_console_line(line_prompt) == "[12:00:00 INFO]: Hello world"


def test_console_filter():
    lines = [
        "[12:00:00 INFO]: [Server thread/INFO]: Player Steve joined the game",
        "[12:00:01 INFO]: [Server thread/INFO]: There are 3 of a max 120 players online: A, B, C",
        "[12:00:02 INFO]: [Server thread/INFO]: TPS from last 1m, 5m, 15m: 20.0, 20.0, 20.0",
        "[12:00:03 INFO]: [Rcon: Issued server command: execute in minecraft:overworld run tellraw @a {\"text\":\"[Weihnachtsdorf] Broadcast\"}]",
        "[12:00:04 INFO]: [Rcon: execute in minecraft:overworld run tellraw @a [{\"text\":\"[D] \"}]]",
        "[12:00:05 INFO]: [Server thread/INFO]: \u001b[32mColored ANSI text\u001b[0m",
        "[12:00:06 INFO]: [Rcon: Issued server command: gamemode survival Steve]",
        "[K[14:05:25 INFO]: [37m[[1m[37m[96mL[36mP[0m[37m][0m [36m[97m- [36mWeight: [97mNone[0m",
        ">....[?1h=[?2004h>....",
        "System chat: [91mNo player was found[0m",
    ]

    filtered = chat_bridge.console_filter(lines)

    # Bot query / echoes must be removed
    assert not any("There are " in l and "players online" in l for l in filtered)
    assert not any("TPS from last" in l for l in filtered)
    assert not any("tellraw" in l.lower() and "[weihnachtsdorf]" in l.lower() for l in filtered)
    assert not any("tellraw" in l.lower() and "[d]" in l.lower() for l in filtered)
    assert not any("no player was found" in l.lower() for l in filtered)
    assert not any(">...." in l for l in filtered)

    # Legitimate lines, cleaned ANSI, and admin Rcon commands must remain
    assert any("Player Steve joined" in l for l in filtered)
    assert any("Colored ANSI text" in l for l in filtered)
    assert not any("\u001b" in l for l in filtered)
    assert any("gamemode survival Steve" in l for l in filtered)
    assert any("[14:05:25 INFO]: [LP] - Weight: None" == l for l in filtered)


def test_console_filter_user_examples():
    lines = [
        "[K[14:05:25 INFO]: [37m[[1m[37m[96mL[36mP[0m[37m][0m [36m[97m- [36mWeight: [97mNone[0m",
        ">....[?1h=[?2004h>....",
        "System chat: [91mNo player was found[0m",
        "[14:05:26 INFO]: Normal message",
    ]
    filtered = chat_bridge.console_filter(lines)
    assert filtered == [
        "[14:05:25 INFO]: [LP] - Weight: None",
        "[14:05:26 INFO]: Normal message",
    ]


# ---------------------------------------------------------------------------
# chunk_code_blocks tests
# ---------------------------------------------------------------------------
def test_chunk_code_blocks():
    # Empty input
    assert chat_bridge.chunk_code_blocks([]) == []

    # Small lines grouped into 1 block
    lines = ["Line 1", "Line 2", "Line 3"]
    chunks = chat_bridge.chunk_code_blocks(lines, limit=100)
    assert len(chunks) == 1
    assert chunks[0] == "```\nLine 1\nLine 2\nLine 3\n```"
    assert len(chunks[0]) <= 100

    # Many lines exceeding limit
    lines = [f"Log entry number {i:03d} with detailed log information" for i in range(100)]
    chunks = chat_bridge.chunk_code_blocks(lines, limit=500)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 500
        assert c.startswith("```\n")
        assert c.endswith("\n```")

    # Single oversized line truncated to fit limit
    oversized = ["X" * 3000]
    chunks = chat_bridge.chunk_code_blocks(oversized, limit=1900)
    assert len(chunks) == 1
    assert len(chunks[0]) <= 1900
    assert chunks[0].startswith("```\n")
    assert chunks[0].endswith("\n```")


def test_chunk_code_blocks_escapes_triple_backticks():
    lines = ["Player said: ```@everyone```", "Normal line"]
    chunks = chat_bridge.chunk_code_blocks(lines, limit=1900)
    assert len(chunks) == 1
    body = chunks[0][4:-4]
    assert "```" not in body
    assert "'''@everyone'''" in body


# ---------------------------------------------------------------------------
# active_chat_channel_id tests
# ---------------------------------------------------------------------------
def test_active_chat_channel_id_prelaunch_and_live():
    with patch("wd_identity.chat_bridge.MinecraftSyncClient") as mock_mc_cls:
        mock_mc = mock_mc_cls.return_value

        # Pre-launch
        mock_mc.is_project_launched.return_value = False
        chan_pre = chat_bridge.active_chat_channel_id()
        expected_pre = str(getattr(settings, "DISCORD_CHANNEL_CHAT_PRELAUNCH_ID", "1570031673809043476"))
        assert chan_pre == expected_pre

        # Post-launch (Live)
        mock_mc.is_project_launched.return_value = True
        chan_live = chat_bridge.active_chat_channel_id()
        expected_live = str(getattr(settings, "DISCORD_CHANNEL_CHAT_LIVE_ID", "913851557102112799"))
        assert chan_live == expected_live


# ---------------------------------------------------------------------------
# enqueue_to_discord tests
# ---------------------------------------------------------------------------
def test_enqueue_to_discord_ignores_source_discord():
    async def _run():
        q = chat_bridge._get_queue()
        while not q.empty():
            q.get_nowait()

        event = {"source": "discord", "type": "chat", "message": "Ignored message"}
        await chat_bridge.enqueue_to_discord(event)
        assert q.qsize() == 0

    asyncio.run(_run())


def test_enqueue_to_discord_queue_overflow_drops_oldest():
    async def _run():
        q = chat_bridge._get_queue()
        while not q.empty():
            q.get_nowait()

        for i in range(200):
            await chat_bridge.enqueue_to_discord({"type": "chat", "message": f"msg-{i}"})

        assert q.qsize() == 200

        await chat_bridge.enqueue_to_discord({"type": "chat", "message": "msg-overflow"})
        assert q.qsize() == 200

        first_item = q.get_nowait()
        assert first_item["message"] == "msg-1"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# discord_bot hooks tests: bridge_on_message
# ---------------------------------------------------------------------------
def test_bridge_on_message_chat_registered_player():
    async def _run():
        msg = MagicMock()
        msg.author.bot = False
        msg.webhook_id = None
        msg.author.id = 12345
        msg.guild.id = int(settings.DISCORD_GUILD_ID) if settings.DISCORD_GUILD_ID else 909437200125538364
        msg.channel.id = 1570031673809043476
        msg.clean_content = "Hallo aus Discord!"
        msg.attachments = []
        msg.stickers = []

        with patch("wd_identity.chat_bridge.active_chat_channel_id", return_value="1570031673809043476"), \
             patch("wd_identity.chat_bridge.resolve_active_player", return_value=("Tigimax", "uuid-1234")), \
             patch("wd_identity.chat_bridge.resolve_player_by_minecraft", return_value={"role": "player", "discord_id": "12345"}), \
             patch("wd_identity.chat_bridge.rate_ok", return_value=True), \
             patch("wd_identity.discord_bot.MinecraftSyncClient") as mock_mc_cls, \
             patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_http_post:

            mock_mc = mock_mc_cls.return_value
            mock_mc.execute_commands.return_value = True

            await bridge_on_message(msg)

            mock_mc.execute_commands.assert_called_once()
            sent_cmd = mock_mc.execute_commands.call_args[0][0][0]
            assert "execute in minecraft:overworld run tellraw @a" in sent_cmd
            assert "Hallo aus Discord!" in sent_cmd
            assert "<Tigimax>" in sent_cmd

            await asyncio.sleep(0.05)
            mock_http_post.assert_called_once()

    asyncio.run(_run())


def test_bridge_on_message_chat_unregistered_player_reaction():
    async def _run():
        msg = MagicMock()
        msg.author.bot = False
        msg.webhook_id = None
        msg.author.id = 99999
        msg.guild.id = int(settings.DISCORD_GUILD_ID) if settings.DISCORD_GUILD_ID else 909437200125538364
        msg.channel.id = 1570031673809043476
        msg.add_reaction = AsyncMock()

        with patch("wd_identity.chat_bridge.active_chat_channel_id", return_value="1570031673809043476"), \
             patch("wd_identity.chat_bridge.resolve_active_player", return_value=None), \
             patch("wd_identity.chat_bridge.rate_ok", return_value=True), \
             patch("wd_identity.discord_bot.MinecraftSyncClient") as mock_mc_cls:

            mock_mc = mock_mc_cls.return_value
            await bridge_on_message(msg)

            msg.add_reaction.assert_called_once_with("🔒")
            mock_mc.execute_commands.assert_not_called()

    asyncio.run(_run())


def test_bridge_on_message_console_admin_allowed_command():
    async def _run():
        msg = MagicMock()
        msg.author.bot = False
        msg.webhook_id = None
        msg.guild.id = int(settings.DISCORD_GUILD_ID) if settings.DISCORD_GUILD_ID else 909437200125538364
        msg.channel.id = 1234567890
        msg.content = "/time set day"

        import discord
        msg.author = MagicMock(spec=discord.Member)
        msg.author.bot = False
        msg.author.guild_permissions.administrator = True
        msg.author.name = "AdminUser"
        msg.author.mention = "@AdminUser"

        with patch("wd_identity.discord_bot.settings") as mock_settings, \
             patch("wd_identity.discord_bot.MinecraftSyncClient") as mock_mc_cls, \
             patch("wd_identity.discord_bot.log_admin_event") as mock_audit:

            mock_settings.DISCORD_CHANNEL_CONSOLE_ID = "1234567890"
            mock_settings.DISCORD_GUILD_ID = str(msg.guild.id)
            mock_mc = mock_mc_cls.return_value

            await bridge_on_message(msg)

            mock_mc.execute_commands.assert_called_once_with(["time set day"])
            mock_audit.assert_called_once()

    asyncio.run(_run())


def test_bridge_on_message_console_forbidden_command():
    async def _run():
        msg = MagicMock()
        msg.author.bot = False
        msg.webhook_id = None
        msg.guild.id = int(settings.DISCORD_GUILD_ID) if settings.DISCORD_GUILD_ID else 909437200125538364
        msg.channel.id = 1234567890
        msg.content = "/stop"
        msg.reply = AsyncMock()

        import discord
        msg.author = MagicMock(spec=discord.Member)
        msg.author.bot = False
        msg.author.guild_permissions.administrator = True

        with patch("wd_identity.discord_bot.settings") as mock_settings, \
             patch("wd_identity.discord_bot.MinecraftSyncClient") as mock_mc_cls:

            mock_settings.DISCORD_CHANNEL_CONSOLE_ID = "1234567890"
            mock_settings.DISCORD_GUILD_ID = str(msg.guild.id)
            mock_mc = mock_mc_cls.return_value

            await bridge_on_message(msg)

            mock_mc.execute_commands.assert_not_called()
            msg.reply.assert_called_once()
            assert "gesperrt" in msg.reply.call_args[0][0]

    asyncio.run(_run())


def test_bridge_on_message_skips_active_chat_lookup_for_unrelated_channels():
    async def _run():
        msg = MagicMock()
        msg.author.bot = False
        msg.webhook_id = None
        msg.guild.id = int(settings.DISCORD_GUILD_ID) if settings.DISCORD_GUILD_ID else 909437200125538364
        msg.channel.id = 5555555555

        with patch("wd_identity.chat_bridge.active_chat_channel_id") as mock_active_id:
            await bridge_on_message(msg)
            mock_active_id.assert_not_called()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# diff_new_log_lines test
# ---------------------------------------------------------------------------
def test_diff_new_log_lines():
    # Empty prev => returns []
    assert new_log_lines([], ["line 1", "line 2"]) == []

    # Overlap diff
    prev = ["a", "b", "c", "d"]
    cur = ["c", "d", "e", "f"]
    assert new_log_lines(prev, cur) == ["e", "f"]

    # No overlap
    prev = ["a", "b"]
    cur = ["c", "d"]
    assert new_log_lines(prev, cur) == ["c", "d"]


# ---------------------------------------------------------------------------
# Webhook dispatch formatting & sanitization tests
# ---------------------------------------------------------------------------
def test_sanitize_webhook_username():
    assert chat_bridge.sanitize_webhook_username("NormalPlayer") == "NormalPlayer"
    assert "d!scord" in chat_bridge.sanitize_webhook_username("DiscordMod").lower()
    assert "clyd_e" in chat_bridge.sanitize_webhook_username("ClydeBot").lower()
    assert len(chat_bridge.sanitize_webhook_username("A" * 120)) == 80


def test_webhook_event_formatting_and_429_retry():
    async def _run():
        mock_client = AsyncMock()

        # Mock 429 then 200
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.json.return_value = {"retry_after": 0.01}

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200

        mock_client.post.side_effect = [mock_resp_429, mock_resp_200]

        with patch("wd_identity.chat_bridge._get_channel_webhook_url", return_value="https://discord.com/api/webhooks/123/token"), \
             patch("wd_identity.chat_bridge.active_chat_channel_id", return_value="1570031673809043476"), \
             patch("wd_identity.chat_bridge.resolve_player_by_minecraft", return_value={"discord_id": "123456789", "role": "admin", "ign": "WebUser", "uuid": ""}), \
             patch("wd_identity.chat_bridge.get_discord_avatar_url", return_value="https://cdn.discordapp.com/avatars/123456789/avatar123.png?size=128"):

            # 1. Webchat event -> must NOT have 🌐 prefix and must have discord avatar
            event = {
                "type": "webchat",
                "username": "WebUser",
                "uuid": "00000000-1111-2222-3333-444455556666",
                "message": "Hello from web",
            }
            await chat_bridge._send_event_to_webhook(mock_client, event)

            # Two calls because of 429 retry
            assert mock_client.post.call_count == 2
            posted_json = mock_client.post.call_args_list[0][1]["json"]
            assert posted_json["content"] == "Hello from web"
            assert posted_json["avatar_url"] == "https://cdn.discordapp.com/avatars/123456789/avatar123.png?size=128"

            # 2. Join event
            mock_client.post.reset_mock()
            mock_client.post.return_value = mock_resp_200
            mock_client.post.side_effect = None
            join_ev = {"type": "join", "username": "Steve", "uuid": ""}
            await chat_bridge._send_event_to_webhook(mock_client, join_ev)
            posted_join = mock_client.post.call_args[1]["json"]
            assert posted_join["content"] == "➕ **Steve** hat den Server betreten"
            assert "discordapp.com" in posted_join["avatar_url"]

            # 3. Quit event
            mock_client.post.reset_mock()
            quit_ev = {"type": "quit", "username": "Alex", "uuid": "0"}
            await chat_bridge._send_event_to_webhook(mock_client, quit_ev)
            posted_quit = mock_client.post.call_args[1]["json"]
            assert posted_quit["content"] == "➖ **Alex** hat den Server verlassen"
            assert "discordapp.com" in posted_quit["avatar_url"]

            # 4. Death event
            mock_client.post.reset_mock()
            death_ev = {"type": "death", "username": "Alex", "message": "Alex fell from a high place"}
            await chat_bridge._send_event_to_webhook(mock_client, death_ev)
            posted_death = mock_client.post.call_args[1]["json"]
            assert posted_death["content"] == "💀 Alex fell from a high place"

    asyncio.run(_run())


def test_resolve_player_by_minecraft_admin_mod_player():
    with patch("wd_identity.chat_bridge.LDAPClient") as mock_ldap_cls:
        mock_ldap = mock_ldap_cls.return_value

        # Case 1: Admin
        mock_ldap.search_by_minecraft.return_value = {
            "uid": "admin_user",
            "dn": "uid=admin_user,ou=User,dc=ldap-01,dc=net",
            "minecraftName": "AdminSteve",
            "minecraftUuid": "uuid-admin",
            "discordId": "111111111",
        }
        mock_ldap.get_group_members.side_effect = lambda g: (
            ["uid=admin_user,ou=User,dc=ldap-01,dc=net"] if "wd_admins" in g else []
        )

        res_admin = chat_bridge.resolve_player_by_minecraft("AdminSteve")
        assert res_admin["role"] == "admin"
        assert res_admin["discord_id"] == "111111111"

        # Case 2: Mod
        chat_bridge._minecraft_player_cache.clear()
        mock_ldap.search_by_minecraft.return_value = {
            "uid": "mod_user",
            "dn": "uid=mod_user,ou=User,dc=ldap-01,dc=net",
            "minecraftName": "ModSteve",
            "minecraftUuid": "uuid-mod",
            "discordId": "222222222",
        }
        mock_ldap.get_group_members.side_effect = lambda g: (
            ["uid=mod_user,ou=User,dc=ldap-01,dc=net"] if "wd_mods" in g else []
        )

        res_mod = chat_bridge.resolve_player_by_minecraft("ModSteve")
        assert res_mod["role"] == "mod"
        assert res_mod["discord_id"] == "222222222"

        # Case 3: Player
        chat_bridge._minecraft_player_cache.clear()
        mock_ldap.search_by_minecraft.return_value = {
            "uid": "player_user",
            "dn": "uid=player_user,ou=User,dc=ldap-01,dc=net",
            "minecraftName": "PlayerSteve",
            "minecraftUuid": "uuid-player",
            "discordId": "333333333",
        }
        mock_ldap.get_group_members.side_effect = lambda g: (
            ["uid=player_user,ou=User,dc=ldap-01,dc=net"] if "wd_players" in g else []
        )

        res_player = chat_bridge.resolve_player_by_minecraft("PlayerSteve")
        assert res_player["role"] == "player"
        assert res_player["discord_id"] == "333333333"

        # Case 4: Cache hit
        mock_ldap.search_by_minecraft.reset_mock()
        res_cached = chat_bridge.resolve_player_by_minecraft("PlayerSteve")
        assert res_cached["role"] == "player"
        mock_ldap.search_by_minecraft.assert_not_called()

        # Case 5: Unlinked / unknown
        chat_bridge._minecraft_player_cache.clear()
        mock_ldap.search_by_minecraft.return_value = None
        mock_ldap.get_user.return_value = None
        res_unknown = chat_bridge.resolve_player_by_minecraft("UnknownGuy")
        assert res_unknown["role"] == "player"
        assert res_unknown["discord_id"] == ""


def test_get_discord_avatar_url():
    async def _run():
        mock_client = AsyncMock()

        with patch("wd_identity.chat_bridge.settings.DISCORD_BOT_TOKEN", "mock_bot_token"), \
             patch("wd_identity.chat_bridge.settings.DISCORD_GUILD_ID", "909437200125538364"):

            # 1. Guild avatar
            mock_resp_guild = MagicMock()
            mock_resp_guild.status_code = 200
            mock_resp_guild.json.return_value = {
                "avatar": "guild_hash_123",
                "user": {"avatar": "user_hash_456"},
            }
            mock_client.get.return_value = mock_resp_guild

            url_guild = await chat_bridge.get_discord_avatar_url(mock_client, "123456789")
            assert "guilds/" in url_guild
            assert "guild_hash_123.png?size=128" in url_guild

            # 2. User avatar (when no guild avatar)
            chat_bridge._avatar_cache.clear()
            mock_resp_user = MagicMock()
            mock_resp_user.status_code = 200
            mock_resp_user.json.return_value = {
                "avatar": None,
                "user": {"avatar": "user_hash_789"},
            }
            mock_client.get.return_value = mock_resp_user

            url_user = await chat_bridge.get_discord_avatar_url(mock_client, "987654321")
            assert "avatars/987654321/user_hash_789.png?size=128" in url_user

            # 3. Default embed avatar (no custom avatar)
            chat_bridge._avatar_cache.clear()
            mock_resp_default = MagicMock()
            mock_resp_default.status_code = 200
            mock_resp_default.json.return_value = {"avatar": None, "user": {"avatar": None}}
            mock_client.get.return_value = mock_resp_default

            url_default = await chat_bridge.get_discord_avatar_url(mock_client, "111222333444")
            assert "embed/avatars/" in url_default

            # 4. Fallback for unlinked / empty discord_id
            url_empty = await chat_bridge.get_discord_avatar_url(mock_client, "")
            assert url_empty == "https://cdn.discordapp.com/embed/avatars/0.png"

    asyncio.run(_run())


def test_webhook_deleted_or_invalid_status_clears_cache():
    async def _run():
        mock_client = AsyncMock()
        mock_resp_404 = MagicMock()
        mock_resp_404.status_code = 404
        mock_resp_404.text = "Unknown Webhook"
        mock_client.post.return_value = mock_resp_404

        chan_id = "1570031673809043476"
        chat_bridge._webhook_cache[chan_id] = "https://discord.com/api/webhooks/old/token"

        with patch("wd_identity.chat_bridge.active_chat_channel_id", return_value=chan_id), \
             patch("wd_identity.chat_bridge.resolve_player_by_minecraft", return_value={"role": "player", "discord_id": ""}):
            event = {"type": "chat", "username": "Steve", "message": "hello"}
            await chat_bridge._send_event_to_webhook(mock_client, event)

            assert chan_id not in chat_bridge._webhook_cache

    asyncio.run(_run())


def test_webhook_join_quit_escapes_markdown_in_username():
    async def _run():
        mock_client = AsyncMock()
        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_client.post.return_value = mock_resp_200

        with patch("wd_identity.chat_bridge._get_channel_webhook_url", return_value="https://discord.com/api/webhooks/123/token"), \
             patch("wd_identity.chat_bridge.active_chat_channel_id", return_value="1570031673809043476"), \
             patch("wd_identity.chat_bridge.resolve_player_by_minecraft", return_value={"role": "player", "discord_id": ""}):

            # Join with markdown characters in name
            join_ev = {"type": "join", "username": "_Underline*Star~Tilde_", "uuid": ""}
            await chat_bridge._send_event_to_webhook(mock_client, join_ev)
            posted_join = mock_client.post.call_args[1]["json"]
            assert posted_join["content"] == r"➕ **\_Underline\*Star\~Tilde\_** hat den Server betreten"

            # Quit with markdown characters in name
            mock_client.post.reset_mock()
            quit_ev = {"type": "quit", "username": "*Alex*", "uuid": "0"}
            await chat_bridge._send_event_to_webhook(mock_client, quit_ev)
            posted_quit = mock_client.post.call_args[1]["json"]
            assert posted_quit["content"] == r"➖ **\*Alex\*** hat den Server verlassen"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Strict admin check and console_log_sync_loop tests
# ---------------------------------------------------------------------------
def test_bridge_on_message_console_arbitrary_role_named_admin_rejected():
    async def _run():
        msg = MagicMock()
        msg.author.bot = False
        msg.webhook_id = None
        msg.guild.id = int(settings.DISCORD_GUILD_ID) if settings.DISCORD_GUILD_ID else 909437200125538364
        msg.channel.id = 1234567890
        msg.content = "whitelist list"

        import discord
        msg.author = MagicMock(spec=discord.Member)
        msg.author.bot = False
        msg.author.guild_permissions.administrator = False
        fake_role = MagicMock()
        fake_role.id = 999999  # Not DISCORD_ADMIN_ROLE_ID
        fake_role.name = "Admin"
        msg.author.roles = [fake_role]

        with patch("wd_identity.discord_bot.settings") as mock_settings, \
             patch("wd_identity.discord_bot.MinecraftSyncClient") as mock_mc_cls:

            mock_settings.DISCORD_CHANNEL_CONSOLE_ID = "1234567890"
            mock_settings.DISCORD_ADMIN_ROLE_ID = "909437200125538371"
            mock_settings.DISCORD_GUILD_ID = str(msg.guild.id)
            mock_mc = mock_mc_cls.return_value

            await bridge_on_message(msg)

            # Role named Admin without administrator permission or DISCORD_ADMIN_ROLE_ID must be ignored
            mock_mc.execute_commands.assert_not_called()

    asyncio.run(_run())


def test_console_log_sync_loop_streaming():
    async def _run():
        channel = AsyncMock()

        with patch("wd_identity.discord_bot.settings") as mock_settings, \
             patch("wd_identity.discord_bot.bot") as mock_bot, \
             patch("wd_identity.discord_bot.MinecraftSyncClient") as mock_mc_cls, \
             patch("wd_identity.discord_bot._last_log", ["line 1"]), \
             patch("wd_identity.discord_bot.new_log_lines", return_value=["line 2", "line 3"]):

            mock_settings.DISCORD_CHANNEL_CONSOLE_ID = "1234567890"
            mock_bot.get_channel.return_value = channel

            mock_mc = mock_mc_cls.return_value
            mock_mc.get_log_lines.return_value = ["line 1", "line 2", "line 3"]

            # Trigger loop iteration directly
            await console_log_sync_loop.coro()

            channel.send.assert_called_once()
            sent_content = channel.send.call_args[0][0]
            assert "line 2" in sent_content
            assert "line 3" in sent_content

    asyncio.run(_run())


def test_cmd_mc_befehl_non_admin_rejected():
    async def _run():
        interaction = AsyncMock()
        member = MagicMock(spec=discord.Member)
        member.guild_permissions.administrator = False
        member.roles = []
        interaction.user = member

        await cmd_mc_befehl.callback(interaction, "whitelist list")

        interaction.response.send_message.assert_called_once()
        args, kwargs = interaction.response.send_message.call_args
        assert "Nur Administratoren" in args[0]
        assert kwargs.get("ephemeral") is True

    asyncio.run(_run())


def test_cmd_mc_befehl_empty_command_rejected():
    async def _run():
        interaction = AsyncMock()
        member = MagicMock(spec=discord.Member)
        member.guild_permissions.administrator = True
        interaction.user = member

        await cmd_mc_befehl.callback(interaction, "   /   ")

        interaction.response.send_message.assert_called_once()
        args, kwargs = interaction.response.send_message.call_args
        assert "gültigen Konsolenbefehl" in args[0]
        assert kwargs.get("ephemeral") is True

    asyncio.run(_run())


def test_cmd_mc_befehl_success_with_output_and_filtering():
    async def _run():
        interaction = AsyncMock()
        member = MagicMock(spec=discord.Member)
        member.guild_permissions.administrator = True
        member.name = "TestAdmin"
        member.mention = "@TestAdmin"
        interaction.user = member

        with patch("wd_identity.discord_bot.MinecraftSyncClient") as mock_mc_cls, \
             patch("wd_identity.discord_bot.log_admin_event") as mock_audit:

            mock_mc = mock_mc_cls.return_value
            # Returns valid command output mixed with bot query echo
            mock_mc.run_and_capture.return_value = [
                "[12:00:00 INFO]: There are 2 whitelisted players: Alice, Bob",
                "[12:00:00 INFO]: There are 0 of a max of 120 players online",
            ]

            await cmd_mc_befehl.callback(interaction, "/whitelist list")

            mock_mc.run_and_capture.assert_called_once_with("whitelist list")
            interaction.response.defer.assert_called_once_with(ephemeral=False)
            interaction.followup.send.assert_called_once()
            mock_audit.assert_called_once()

            kwargs = interaction.followup.send.call_args[1]
            embed = kwargs.get("embed")
            assert embed is not None
            assert embed.title == "💻 Minecraft Konsole"
            assert "whitelist list" in embed.description
            assert embed.color == discord.Color.green()

            fields = {f.name: f.value for f in embed.fields}
            assert "Status" in fields
            assert "✅" in fields["Status"]
            assert "Ausgabe" in fields
            # Filter should remove "There are 0 of a max of 120 players online"
            assert "There are 2 whitelisted players: Alice, Bob" in fields["Ausgabe"]
            assert "players online" not in fields["Ausgabe"]

    asyncio.run(_run())


def test_cmd_mc_befehl_empty_output_handled():
    async def _run():
        interaction = AsyncMock()
        member = MagicMock(spec=discord.Member)
        member.guild_permissions.administrator = True
        member.name = "TestAdmin"
        member.mention = "@TestAdmin"
        interaction.user = member

        with patch("wd_identity.discord_bot.MinecraftSyncClient") as mock_mc_cls, \
             patch("wd_identity.discord_bot.log_admin_event"):

            mock_mc = mock_mc_cls.return_value
            mock_mc.run_and_capture.return_value = []

            await cmd_mc_befehl.callback(interaction, "weather clear")

            interaction.followup.send.assert_called_once()
            embed = interaction.followup.send.call_args[1]["embed"]
            fields = {f.name: f.value for f in embed.fields}
            assert "Ausgabe" in fields
            assert fields["Ausgabe"] == "*(Keine Konsolenausgabe)*"

    asyncio.run(_run())


def test_cmd_mc_befehl_long_output_truncated():
    async def _run():
        interaction = AsyncMock()
        member = MagicMock(spec=discord.Member)
        member.guild_permissions.administrator = True
        member.name = "TestAdmin"
        member.mention = "@TestAdmin"
        interaction.user = member

        with patch("wd_identity.discord_bot.MinecraftSyncClient") as mock_mc_cls, \
             patch("wd_identity.discord_bot.log_admin_event"):

            mock_mc = mock_mc_cls.return_value
            # Generate 2000+ chars of output
            mock_mc.run_and_capture.return_value = ["Line " + str(i) + " " + "x" * 50 for i in range(40)]

            await cmd_mc_befehl.callback(interaction, "help")

            interaction.followup.send.assert_called_once()
            embed = interaction.followup.send.call_args[1]["embed"]
            fields = {f.name: f.value for f in embed.fields}
            assert "Ausgabe" in fields
            # Must fit in Discord field limits (<= 1024)
            assert len(fields["Ausgabe"]) <= 1024
            assert fields["Ausgabe"].endswith("...```")

    asyncio.run(_run())


def test_cmd_mc_befehl_execution_error():
    async def _run():
        interaction = AsyncMock()
        member = MagicMock(spec=discord.Member)
        member.guild_permissions.administrator = True
        member.name = "TestAdmin"
        member.mention = "@TestAdmin"
        interaction.user = member

        with patch("wd_identity.discord_bot.MinecraftSyncClient") as mock_mc_cls, \
             patch("wd_identity.discord_bot.log_admin_event"):

            mock_mc = mock_mc_cls.return_value
            mock_mc.run_and_capture.side_effect = RuntimeError("Connection to Wings failed")

            await cmd_mc_befehl.callback(interaction, "tps")

            interaction.followup.send.assert_called_once()
            embed = interaction.followup.send.call_args[1]["embed"]
            assert embed.color == discord.Color.red()
            fields = {f.name: f.value for f in embed.fields}
            assert "Status" in fields
            assert "❌" in fields["Status"]

    asyncio.run(_run())


