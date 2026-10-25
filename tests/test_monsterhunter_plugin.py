"""Monster Hunter plugin flow tests."""

from dataclasses import replace
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from opensnap.config import StorageConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.core.sessions import Session
from opensnap.plugins.monsterhunter import MonsterHunterPlugin
from opensnap.plugins.monsterhunter.directory import Directory, read_directory
import opensnap.plugins.monsterhunter.plugin as plugin_module
from opensnap.plugins.monsterhunter.profile import MONSTER_HUNTER_NA, MONSTER_HUNTER_NA_BETA
from opensnap.plugins.monsterhunter_na_beta import MonsterHunterNaBetaPlugin
from opensnap.protocol import commands
from opensnap.protocol.codec import decode_datagram, encode_messages
from opensnap.protocol.constants import (
    FLAG_CHANNEL_BITS,
    FLAG_LOBBY,
    FLAG_MULTI,
    FLAG_RELAY,
    FLAG_RELIABLE,
    FLAG_RESPONSE,
    FLAG_ROOM,
)
from opensnap.protocol.models import Endpoint, SnapMessage
from opensnap.core.records import Ranking
from opensnap.storage.sql import SqlOnlinePlayerStore, SqlRecordStore
from opensnap.storage.sqlite import SqliteConnection
from opensnap_app.capcom.monsterhunter.content import EventCatalog, InformationPages, MarketState
from opensnap_app.capcom.monsterhunter.flows import AppFlowTracker, AppPhase
from opensnap_app.capcom.monsterhunter.records import (
    RECORD_PAGE_SAFE_CELLS,
    RECORD_PAGE_SAFE_SIZE,
    RecordPages,
    load_quests,
)
from opensnap_app.capcom.monsterhunter.patch import load_patches
from opensnap_app.capcom.monsterhunter.ram_patch import BLOB_MAGIC, build_na_ram_patch, build_ram_patch
from opensnap_app.capcom.monsterhunter.service import (
    APP_BUILD_EU,
    APP_BUILD_NA,
    NA_PUBLIC_BETA_VERSION,
    AppService,
    AppServiceConfig,
)
from opensnap_app.capcom.protocol import (
    app_frame,
    decode_app_field,
    encode_app_field,
    frame_command,
    frame_payload,
    frame_sequence,
    pop_app_frame,
)
from opensnap_app.capcom.server import CapcomAppServer
from tests.support import MONSTER_HUNTER_NA_TITLE_CODE, create_accounts, login_client_payload, serving

TOWN_RULES = (3 << 8) | 4
QUEST_RULES = 2
QUEST_STARTED_RULES = 0x40000002
AREA_ID = 1
# Build stamp of the TLES_527.07 test build's APP answer (`0x00397ce0`).
TLES_STAMP = b'0408131008'
# Build stamp of the SLED_530.83 demo (`0x0035ad60`), which answers like EU.
SLED_STAMP = b'0412241846'
EU_STOCK_STAMP = b'0502180939'
NA_STOCK_STAMP = b'0407251750'
# The patches the repository ships, as `tools/monsterhunter` built them.
DATA_DIRECTORY = Path(__file__).resolve().parents[1] / 'data' / 'monsterhunter'


def _copy_patches(data_directory: Path, *, enabled: bool = True) -> None:
    """Copy the built patches into a test data directory, ours set to `enabled` (Capcom's stays disabled)."""

    shutil.copytree(DATA_DIRECTORY / 'patches', data_directory / 'patches')
    manifest_path = data_directory / 'patches' / 'patches.json'
    manifest = json.loads(manifest_path.read_text())
    for build, entry in manifest.items():
        entry['enabled'] = enabled and build != 'eu_official'
    manifest_path.write_text(json.dumps(manifest))


# The built patches are disabled (they cannot install without a signed DNAS
# header); the delivery tests serve an enabled copy.
_ENABLED_PATCHES = tempfile.TemporaryDirectory()
_copy_patches(Path(_ENABLED_PATCHES.name))
PATCHES = load_patches(Path(_ENABLED_PATCHES.name))
EU_PATCH = PATCHES[EU_STOCK_STAMP]
NA_PATCH = PATCHES[NA_STOCK_STAMP]
# `PatchExecCS` keeps the decoded EU blob at `0x0039e300`.
EU_RESIDENT_BLOB = 0x0039E300


class MonsterHunterFlowTests(unittest.TestCase):
    """Town, quest departure and return flow through the protocol engine."""

    plugin_class: type[MonsterHunterPlugin] = MonsterHunterPlugin

    def setUp(self) -> None:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        config = serving(default_app_config(), self.plugin_class.name)
        # The bootstrap serves this game only, as a deployment does for the beta, whose
        # title code is the release's (`OPENSNAP_BOOTSTRAP_GAMES`).
        self.config = replace(
            config,
            server=replace(config.server, bootstrap_games=(self.plugin_class.name,)),
            storage=StorageConfig(backend='sqlite', sqlite_path=f'{temp_directory.name}/mh.sqlite'),
        )
        create_accounts(self.config, *(f'hunter{index}' for index in range(4)))
        self.plugin = self.plugin_class()
        self.layout = self.plugin.profile.member_profile
        self.engine = SnapProtocolEngine(config=self.config, plugin=self.plugin, role='combined')
        self.addCleanup(self.engine.close)
        self.sequences: dict[int, int] = {}
        self.unreliable_sequences: dict[int, int] = {}
        self.players = [self._login(f'hunter{index}', f'10.0.0.{index + 1}') for index in range(3)]

    def _login(self, username: str, host: str) -> tuple[Endpoint, int]:
        endpoint = Endpoint(host=host, port=5000)
        login = self._send_raw(
            endpoint,
            0,
            commands.CMD_LOGIN_CLIENT,
            login_client_payload(username.encode(), MONSTER_HUNTER_NA_TITLE_CODE),
            FLAG_CHANNEL_BITS,
        )
        session_id = login[0].session_id
        kics = self._send((endpoint, session_id), commands.CMD_LOGIN_TO_KICS, bytes(0x130), FLAG_CHANNEL_BITS)
        self.assertEqual(kics[0].command, commands.CMD_RESULT_LOGIN_TO_KICS)
        self._send((endpoint, session_id), commands.CMD_JOIN, struct.pack('>L', AREA_ID), FLAG_CHANNEL_BITS)
        return endpoint, session_id

    def _send_raw(self, endpoint, session_id, command, payload, type_flags, sequence=None) -> list[SnapMessage]:
        if sequence is None:
            # The client numbers reliable and unreliable packets separately.
            counters = self.sequences if type_flags & FLAG_RELIABLE else self.unreliable_sequences
            sequence = counters.get(session_id, 0) + 1
            counters[session_id] = sequence
        message = SnapMessage(
            endpoint=endpoint,
            type_flags=type_flags,
            packet_number=0,
            command=command,
            session_id=session_id,
            sequence_number=sequence,
            acknowledge_number=0,
            payload=payload,
        )
        result = self.engine.handle_datagram(encode_messages([message]), endpoint)
        self.assertFalse(result.errors)
        if command == commands.CMD_LOGIN_TO_KICS:
            # The client restarts its reliable numbering at 0 when it sends the KICS login.
            self.sequences[session_id] = -1
        return result.messages

    def _send(self, player, command, payload=b'', type_flags=FLAG_ROOM | FLAG_RELIABLE, sequence=None):
        endpoint, session_id = player
        return self._send_raw(endpoint, session_id, command, payload, type_flags, sequence)

    def test_kics_from_a_new_source_port_continues_the_bootstrap_login(self) -> None:
        # `logincapture.pcapng`: bootstrap from port 61357, KICS from 61358 with the
        # client's own header session word and its login ID at `+16`.
        bootstrap = Endpoint(host='10.0.0.9', port=61357)
        login = self._send_raw(bootstrap, 0, commands.CMD_LOGIN_CLIENT,
                               login_client_payload(b'hunter3', MONSTER_HUNTER_NA_TITLE_CODE), FLAG_CHANNEL_BITS)
        session_id = login[0].session_id
        kics = bytearray(0x130)
        kics[16:24] = b'hunter3-'

        reply = self._send_raw(Endpoint(host='10.0.0.9', port=61358), 0x026407D0, commands.CMD_LOGIN_TO_KICS,
                               bytes(kics), FLAG_CHANNEL_BITS)

        self.assertEqual([message.command for message in reply], [commands.CMD_RESULT_LOGIN_TO_KICS])
        self.assertEqual(struct.unpack_from('>L', reply[0].payload, 8)[0], session_id)
        self.assertEqual(self.engine._sessions.get(session_id).endpoint.port, 61358)  # noqa: SLF001

        # The same login ID from another host is not that login.
        other = self._send_raw(Endpoint(host='10.0.0.10', port=61358), 0x026407D0, commands.CMD_LOGIN_TO_KICS,
                               bytes(kics), FLAG_CHANNEL_BITS)
        self.assertEqual(other, [])

    def test_logins_and_areas_are_published_for_the_capcom_app(self) -> None:
        online = self.engine._storage.online_players  # noqa: SLF001
        players = online.list(self.plugin.name)
        self.assertEqual([(player.username, player.host, player.area_id) for player in players], [
            ('hunter0', '10.0.0.1', AREA_ID), ('hunter1', '10.0.0.2', AREA_ID), ('hunter2', '10.0.0.3', AREA_ID),
        ])

        endpoint, session_id = self.players[0]
        self._send((endpoint, session_id), commands.CMD_LEAVE, b'', FLAG_CHANNEL_BITS)
        # A repeated KICS (Land entry) is a new login for the APP flow tracker.
        self._send((endpoint, session_id), commands.CMD_LOGIN_TO_KICS, bytes(0x130), FLAG_CHANNEL_BITS)
        again = {player.session_id: player for player in online.list(self.plugin.name)}[session_id]
        self.assertEqual(again.area_id, 0)
        self.assertGreater(again.login_serial, players[0].login_serial)

        self._send((endpoint, session_id), commands.CMD_LOGOUT_CLIENT, b'', FLAG_CHANNEL_BITS)
        self.assertNotIn(session_id, [player.session_id for player in online.list(self.plugin.name)])

    def test_embedded_child_sequence_word_does_not_move_the_session_sequence(self) -> None:
        # Live NA multi: outer reliable 0x0c seq 17 plus an embedded CMD_SEND whose
        # header `+0x08` reads `05 00 00 00`. Only the outer sequence counts, so the
        # following quest-start commands are not taken for duplicates.
        endpoint, session_id = player = self.players[0]
        flags = FLAG_ROOM | FLAG_RELIABLE
        outer = SnapMessage(
            endpoint=endpoint,
            type_flags=flags | FLAG_MULTI,
            packet_number=4,
            command=commands.CMD_CHANGE_USER_PROPERTY,
            session_id=session_id,
            sequence_number=17,
            acknowledge_number=4,
            payload=self._profile(1),
            size_word_override=(flags | FLAG_MULTI) | (16 + len(self._profile(1))),
        )
        child = SnapMessage(
            endpoint=endpoint,
            type_flags=flags,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=0x05000000,
            acknowledge_number=0,
            payload=bytes.fromhex('09f0010000000000030000'),
        )
        result = self.engine.handle_datagram(encode_messages([outer, child]), endpoint)
        self.assertFalse(result.errors)
        session = self.engine._sessions.get(session_id)  # noqa: SLF001
        self.assertEqual(session.last_incoming_sequence, 17)
        # No reply acknowledges the child's word.
        self.assertNotIn(0x05000000, [message.acknowledge_number for message in result.messages])

        # Quest release: outer CMD_SEND seq 18 with an embedded profile whose word
        # reads 27; its result acknowledges the outer packet.
        release = SnapMessage(
            endpoint=endpoint,
            type_flags=flags | FLAG_MULTI,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=session_id,
            sequence_number=18,
            acknowledge_number=8,
            payload=bytes.fromhex('04f0100000000000'),
            size_word_override=(flags | FLAG_MULTI) | 24,
        )
        profile = replace(child, command=commands.CMD_CHANGE_USER_PROPERTY, sequence_number=27, payload=self._profile(1))
        result = self.engine.handle_datagram(encode_messages([release, profile]), endpoint)
        self.assertFalse(result.errors)
        wrappers = [message for message in result.messages if message.command == commands.CMD_RESULT_WRAPPER]
        self.assertEqual([wrapper.acknowledge_number for wrapper in wrappers], [18])
        self.assertEqual(session.last_incoming_sequence, 18)

        self.sequences[session_id] = 18
        town_id = self._create(player, TOWN_RULES)
        self.assertEqual(self._roster(player, town_id), [session_id])
        self.assertEqual(session.last_incoming_sequence, self.sequences[session_id])

    def test_unreliable_sequence_does_not_move_the_reliable_session_sequence(self) -> None:
        # Live NA: after a relogin the client's unreliable counter ran ahead (an
        # unreliable CMD_SEND seq 22 while reliable requests were at 15); the next
        # reliable requests (quest-start 0x10 seq 16, leave seq 17) must not be
        # taken for duplicates.
        endpoint, session_id = player = self.players[0]
        session = self.engine._sessions.get(session_id)  # noqa: SLF001
        self._publish(player, self._profile(1))
        before = session.last_incoming_sequence
        self._send(player, commands.CMD_SEND, bytes.fromhex('0000'), FLAG_ROOM, sequence=self.sequences[session_id] + 10)
        self.assertEqual(session.last_incoming_sequence, before)
        town_id = self._create(player, TOWN_RULES)
        self.assertEqual(self._roster(player, town_id), [session_id])
        self.assertEqual(session.last_incoming_sequence, self.sequences[session_id])

    def _profile(self, marker: int, *, departing: bool = False) -> bytes:
        """A member profile in the plugin's layout; departing is `3/5` at its departure offsets."""

        profile = bytearray([marker]) * self.layout.size
        profile[self.layout.party_state_offset] = 3 if departing else 0
        profile[self.layout.quest_state_offset] = 5 if departing else 0
        return bytes(profile)

    def _publish(self, player, profile: bytes) -> list[SnapMessage]:
        return self._send(player, commands.CMD_CHANGE_USER_PROPERTY, profile)

    def _create(self, player, rules: int, max_players: int = 4) -> int:
        payload = bytearray(0x2C)
        payload[0:4] = b'Town'
        payload[0x10:0x14] = struct.pack('>L', max_players)
        payload[0x28:0x2C] = struct.pack('>L', rules)
        messages = self._send(player, commands.CMD_CREATE_GAME_ROOM, bytes(payload))
        wrapper = [message for message in messages if message.command == commands.CMD_RESULT_WRAPPER][0]
        return struct.unpack('>2L', wrapper.payload)[1]

    def _join(self, player, room_id: int) -> list[SnapMessage]:
        return self._send(player, commands.CMD_JOIN, struct.pack('>L', room_id))

    def _stat(self, player, rules: int) -> list[SnapMessage]:
        return self._send(player, commands.CMD_CHANGE_ATTRIBUTE, b'STAT' + struct.pack('>L', rules))

    def _roster(self, player, room_id: int) -> list[int]:
        reply = self._send(player, commands.CMD_QUERY_USER, struct.pack('>L', room_id))[0]
        room, count, _ = struct.unpack_from('>3L', reply.payload)
        self.assertEqual(room, room_id)
        record_size = 24 + self.layout.size
        return [struct.unpack_from('>L', reply.payload, 12 + index * record_size + 16)[0] for index in range(count)]

    def _town_with_residents(self) -> int:
        host, guest, _ = self.players
        for index, player in enumerate(self.players):
            self._publish(player, self._profile(index + 1))
        town_id = self._create(host, TOWN_RULES)
        self._join(guest, town_id)
        return town_id

    def _depart(self, host, guest, town_id: int) -> int:
        """Party departure in the `lobby.bin` order.

        Host: selector 3 to every member, itself first (`0x00611210`), room
        leave, quest room `CREATE`, then its 3/5 profile (`0x00611d1c`).
        Guest: 3/5 profile (`0x0061cb64`), then room leave and join.
        """

        for member in (host, guest):
            self._send(host, commands.CMD_SEND_TARGET, struct.pack('>2L', 1, member[1]) + bytes((0, 0, 3, 0, 0, 0, 0, 0)))
        self._send(host, commands.CMD_LEAVE)
        quest_id = self._create(host, QUEST_RULES)
        self._publish(host, self._profile(1, departing=True))
        self._publish(guest, self._profile(2, departing=True))
        self._send(guest, commands.CMD_LEAVE)
        self._join(guest, quest_id)
        self._stat(host, QUEST_STARTED_RULES)
        return quest_id

    def _town_row_members(self, player, town_id: int) -> int:
        """Member count of the Town's directory row (`_room_row`)."""

        reply = self._send(player, commands.CMD_QUERY_GAME_ROOMS, struct.pack('>L', AREA_ID), FLAG_CHANNEL_BITS)[0]
        count = struct.unpack_from('>L', reply.payload, 8)[0]
        rows = [struct.unpack_from('>16s5L', reply.payload, 12 + index * 36) for index in range(count)]
        return next(row[1] for row in rows if row[5] == town_id)

    def _area_roster(self, player) -> list[bytes]:
        reply = self._send(player, commands.CMD_QUERY_USER, struct.pack('>L', AREA_ID), FLAG_CHANNEL_BITS)[0]
        count = struct.unpack_from('>3L', reply.payload)[1]
        record_size = 24 + self.layout.size
        return [
            reply.payload[12 + index * record_size:28 + index * record_size].rstrip(b'\0') for index in range(count)
        ]

    def test_new_login_from_the_same_endpoint_ends_the_old_session(self) -> None:
        # A console that crashed logs in again (here with another account) from
        # the same address and port; its old hunter must leave the Area.
        for index, player in enumerate(self.players):
            self._publish(player, self._profile(index + 1))
        crashed_endpoint, crashed_id = self.players[0]
        self.assertIn(b'hunter0', self._area_roster(self.players[1]))

        relogged = self._login('hunter3', crashed_endpoint.host)
        self.assertNotEqual(relogged[1], crashed_id)
        self.assertIsNone(self.engine._sessions.get(crashed_id))  # noqa: SLF001
        self._publish(relogged, self._profile(4))
        self.assertEqual(sorted(self._area_roster(self.players[1])), [b'hunter2', b'hunter3'])

    def test_session_follows_its_clients_new_port(self) -> None:
        # `12 10 2026 1754.pcapng`: after 171 s in the Town browser the console's
        # NAT gave it a new public port; answers still arrived, but every relay
        # went to the old port until the transport timed the session out there.
        self._town_with_residents()
        host, (guest_endpoint, guest_id), _ = self.players
        moved = Endpoint(host=guest_endpoint.host, port=guest_endpoint.port + 1)

        keepalive = SnapMessage(endpoint=moved, type_flags=FLAG_ROOM, packet_number=0, command=commands.CMD_SEND,
                                session_id=guest_id, sequence_number=50, acknowledge_number=0,
                                payload=bytes.fromhex('0010'))
        result = self.engine.handle_datagram(encode_messages([keepalive]), moved)
        self.assertEqual(result.endpoint_moves, [(guest_endpoint, moved, guest_id)])

        relays = self._send(host, commands.CMD_SEND, bytes.fromhex('0000'), FLAG_ROOM)
        self.assertEqual([message.endpoint for message in relays if message.session_id == guest_id], [moved])

        # Another host never takes a session over.
        stranger = Endpoint(host='10.0.0.99', port=moved.port)
        result = self.engine.handle_datagram(encode_messages([replace(keepalive, endpoint=stranger)]), stranger)
        self.assertEqual(result.endpoint_moves, [])
        self.assertEqual(self.engine._sessions.get(guest_id).endpoint, moved)  # noqa: SLF001

    def test_silent_sessions_end_after_their_idle_limit(self) -> None:
        now = [1000.0]
        self.engine._clock = lambda: now[0]  # noqa: SLF001
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._depart(host, guest, town_id)
        lobby_only = self._login('hunter3', '10.0.0.9')
        self._send(lobby_only, commands.CMD_LEAVE, b'', FLAG_CHANNEL_BITS)

        # In an Area: 5 minutes without a datagram.
        now[0] += plugin_module.LOBBY_IDLE_LIMIT_SECONDS - 1
        self.engine.tick()
        self.assertIsNotNone(self.engine._sessions.get(waiting[1]))  # noqa: SLF001
        now[0] += 2
        self.engine.tick()
        self.assertIsNone(self.engine._sessions.get(waiting[1]))  # noqa: SLF001
        # On a started quest the client is silent until the quest ends.
        self.assertIsNotNone(self.engine._sessions.get(host[1]))  # noqa: SLF001
        now[0] += plugin_module.QUEST_IDLE_LIMIT_SECONDS
        self.engine.tick()
        self.assertIsNone(self.engine._sessions.get(host[1]))  # noqa: SLF001
        self.assertIsNone(self.engine._sessions.get(guest[1]))  # noqa: SLF001
        # Outside any Area (World/Land selection) no limit applies.
        self.assertIsNotNone(self.engine._sessions.get(lobby_only[1]))  # noqa: SLF001

    def test_relays_carry_sender_header_and_recipient_transport_session(self) -> None:
        town_id = self._town_with_residents()
        sender, receiver, _ = self.players

        relays = [message for message in self._send(sender, commands.CMD_SEND, b'\x0b\xf0' + bytes(11))
                  if message.command == commands.CMD_SEND]

        self.assertEqual(len(relays), 1)
        relay = relays[0]
        self.assertEqual(relay.session_id, receiver[1])
        self.assertEqual(relay.endpoint, receiver[0])
        header = decode_datagram(encode_messages([relay]), relay.endpoint)[0]
        self.assertEqual(header.session_id, sender[1])
        self.assertIn(receiver[1], self._roster(sender, town_id))

    def test_send_target_relay_zeroes_target_and_identifies_sender(self) -> None:
        self._town_with_residents()
        sender, receiver, _ = self.players
        payload = struct.pack('>2L', 1, receiver[1]) + bytes((0, 0, 4, 0, 0, 0, 0, 0)) + bytes(768)

        messages = self._send(sender, commands.CMD_SEND_TARGET, payload)

        relay = [message for message in messages if message.command == commands.CMD_SEND_TARGET][0]
        self.assertEqual(relay.payload[4:8], bytes(4))
        self.assertEqual(relay.source_session_id, sender[1])
        self.assertEqual(relay.session_id, receiver[1])
        # Game packets keep reaching slot 19 (reliable, room channel, no chat bit).
        self.assertEqual(relay.type_flags, FLAG_ROOM | FLAG_RELIABLE)

    def test_quest_room_notice_is_relayed_as_a_lobby_target_chat(self) -> None:
        # Live capture: after its quest room CREATE the host sends `0xb400`
        # `02 <name len> <name> ... 10 "00000016"`; only slot 14 reads it.
        self._town_with_residents()
        host, guest, _ = self.players
        notice = (
            struct.pack('>2L', 1, guest[1]) + bytes((2, 5)) + b'adam\n'.ljust(17, b'\x00')
            + bytes((16,)) + b'00000016'.ljust(16, b'\x00') + bytes(3)
        )

        messages = self._send(host, commands.CMD_SEND_TARGET, notice, FLAG_CHANNEL_BITS | FLAG_RELAY | FLAG_RELIABLE)

        relay = [message for message in messages if message.command == commands.CMD_SEND_TARGET][0]
        self.assertEqual(relay.session_id, guest[1])
        self.assertEqual(relay.type_flags, FLAG_CHANNEL_BITS | FLAG_RELAY | FLAG_RELIABLE)
        self.assertEqual(relay.payload, notice[:4] + bytes(4) + notice[8:])
        self.assertEqual(relay.source_session_id, host[1])

    def test_departure_is_announced_while_the_town_keeps_the_listing(self) -> None:
        # The client leaves its Town with a plain room leave; the members drop it
        # only on the leave callback (`0x00619590`).
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)

        self._publish(host, self._profile(1, departing=True))
        leave = self._send(host, commands.CMD_LEAVE)

        notices = [message for message in leave if message.command == commands.CMD_LEAVE]
        self.assertEqual({notice.endpoint for notice in notices}, {guest[0], waiting[0]})
        self.assertEqual(self._roster(waiting, town_id), sorted((guest[1], waiting[1])))
        # The Town keeps the departing hunter's place for its return, and its
        # count with it: a count of 0 makes the Town select screen create a
        # new Town (`0x005f37a0`).
        attribute = self._send(waiting, commands.CMD_QUERY_ATTRIBUTE, struct.pack('>L', town_id))[0]
        self.assertEqual(struct.unpack('>L4sL', attribute.payload)[2], 3)
        self.assertEqual(self._town_row_members(waiting, town_id), 3)

    def test_solo_host_departure_keeps_its_town_listing(self) -> None:
        # Live NA solo order (opensnap-game.log, 2026-10-11 22:35:25): selector
        # 3 to itself, room leave, quest room CREATE, the 3/5 profile
        # (`0x00611d1c`), then STAT at the quest start (`0x0061ca8c`).
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        preparation = struct.pack('>2L', 1, host[1]) + bytes((0, 0, 3, 0, 0, 0, 0, 0))
        self._send(host, commands.CMD_SEND_TARGET, preparation)

        leave = self._send(host, commands.CMD_LEAVE)

        notices = [message for message in leave if message.command == commands.CMD_LEAVE]
        self.assertEqual({notice.endpoint for notice in notices}, {guest[0], waiting[0]})
        quest_id = self._create(host, QUEST_RULES)
        self._publish(host, self._profile(1, departing=True))
        self._stat(host, QUEST_STARTED_RULES)
        self.assertNotIn(host[1], self._roster(guest, town_id))
        self.assertEqual(self._town_row_members(guest, town_id), 3)

        # Back from the quest, the host returns to the Town that kept its place,
        # and its members get it as an arrival.
        back = self._stat(host, TOWN_RULES)
        arrivals = [message for message in back if message.command == commands.CMD_JOIN]
        self.assertEqual({arrival.endpoint for arrival in arrivals}, {guest[0], waiting[0]})
        self.assertEqual(set(self._roster(host, quest_id)), {host[1], guest[1], waiting[1]})

    def test_town_select_roster_lists_hunters_away_on_a_quest(self) -> None:
        # The select screen's Player List uses the Town join's roster request
        # (`0x006181e0`); only a requester outside the Town gets the listing.
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        self._publish(host, self._profile(1, departing=True))
        self._send(host, commands.CMD_LEAVE)
        quest_id = self._create(host, QUEST_RULES)
        self._stat(host, QUEST_STARTED_RULES)

        self.assertEqual(self._roster(waiting, town_id), sorted((guest[1], waiting[1])))
        self._send(waiting, commands.CMD_LEAVE)
        self.assertEqual(self._roster(waiting, town_id), sorted((host[1], guest[1])))
        self.assertEqual(self._roster(waiting, quest_id), [host[1]])

    def test_preparation_to_another_player_does_not_mark_the_sender(self) -> None:
        town_id = self._town_with_residents()
        host, guest, _ = self.players
        preparation = struct.pack('>2L', 1, host[1]) + bytes((0, 0, 3, 0, 0, 0, 0, 0))
        self._send(guest, commands.CMD_SEND_TARGET, preparation)

        notices = [message for message in self._send(guest, commands.CMD_LEAVE) if message.command == commands.CMD_LEAVE]

        self.assertEqual([notice.endpoint for notice in notices], [host[0]])
        self.assertNotIn(guest[1], self._roster(host, town_id))

    def test_departure_preparation_ends_with_the_next_room_change(self) -> None:
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        preparation = struct.pack('>2L', 1, host[1]) + bytes((0, 0, 3, 0, 0, 0, 0, 0))
        self._send(host, commands.CMD_SEND_TARGET, preparation)
        self._send(host, commands.CMD_LEAVE)
        self._join(host, town_id)

        self._send(host, commands.CMD_LEAVE)

        self.assertNotIn(host[1], self._roster(waiting, town_id))

    def test_logout_in_a_town_announces_the_leave_and_unlists(self) -> None:
        # The client's logout sends only `kkLogout`, without a room leave first.
        town_id = self._town_with_residents()
        host, guest, _ = self.players

        messages = self._send(guest, commands.CMD_LOGOUT_CLIENT, b'', FLAG_CHANNEL_BITS | FLAG_RELIABLE)

        notices = [message for message in messages if message.command == commands.CMD_LEAVE]
        self.assertEqual([notice.endpoint for notice in notices], [host[0]])
        self.assertEqual(notices[0].payload, struct.pack('>L', guest[1]))
        self.assertNotIn(guest[1], self._roster(host, town_id))
        self.assertEqual(self.engine._sessions.get(guest[1]).lobby_id, 0)  # noqa: SLF001

    def test_plain_leave_announces_departure_and_unlists(self) -> None:
        town_id = self._town_with_residents()
        host, guest, _ = self.players

        messages = self._send(guest, commands.CMD_LEAVE)

        notices = [message for message in messages if message.command == commands.CMD_LEAVE]
        self.assertEqual([notice.endpoint for notice in notices], [host[0]])
        self.assertEqual(notices[0].type_flags, FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE)
        self.assertEqual(notices[0].payload, struct.pack('>L', guest[1]))
        self.assertNotIn(guest[1], self._roster(host, town_id))

    def test_quest_return_moves_each_hunter_back_to_its_town(self) -> None:
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        quest_id = self._depart(host, guest, town_id)
        self.assertEqual(self._roster(waiting, town_id), [waiting[1]])

        guest_return = self._stat(guest, TOWN_RULES)

        self.assertEqual([message.command for message in guest_return], [commands.CMD_RESULT_WRAPPER, commands.CMD_JOIN])
        self.assertEqual(guest_return[1].endpoint, waiting[0])
        # The returning client keeps using its quest room id as its Town id.
        self.assertEqual(set(self._roster(guest, quest_id)), {guest[1], waiting[1]})
        relays = [message for message in self._send(guest, commands.CMD_SEND, b'\x0b\xf0' + bytes(11))
                  if message.command == commands.CMD_SEND]
        self.assertEqual([relay.endpoint for relay in relays], [waiting[0]])

        self._stat(host, TOWN_RULES)
        rooms = self._send(waiting, commands.CMD_QUERY_GAME_ROOMS, struct.pack('>L', AREA_ID), FLAG_CHANNEL_BITS)
        room_ids = [struct.unpack_from('>L', rooms[0].payload, 12 + index * 36 + 32)[0]
                    for index in range(struct.unpack_from('>L', rooms[0].payload, 8)[0])]
        self.assertEqual(room_ids, [town_id])

    def test_hunter_lost_during_a_quest_is_announced_on_each_return(self) -> None:
        # A guest timed out mid-quest got no `CMD_LEAVE` (the members run
        # `game.bin`); the host kept it and showed a clone when it logged in again.
        town_id = self._town_with_residents()
        host, guest, _ = self.players
        self._depart(host, guest, town_id)
        session = self.engine._sessions.get(guest[1])  # noqa: SLF001
        timeout = self.engine._plugin.on_session_timeout(self.engine._context, session)  # noqa: SLF001
        self.assertNotIn(commands.CMD_LEAVE, [message.command for message in timeout])

        back = self._stat(host, TOWN_RULES)

        notices = [message for message in back if message.command == commands.CMD_LEAVE]
        self.assertEqual([(notice.endpoint, notice.payload) for notice in notices], [(host[0], struct.pack('>L', guest[1]))])

    def test_quest_return_without_its_town_listing_never_makes_an_unnamed_town(self) -> None:
        # `11 10 2026 1538.pcapng`: a hunter that had lost its Town listing came
        # back from a quest and its STAT turned the nameless quest room into a
        # Town row with the Allsburg rules, which then spread to later quests.
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        quest_id = self._depart(host, guest, town_id)
        self.engine._rooms.leave(town_id, guest[1])  # noqa: SLF001
        quest_rules = self.engine._rooms.get(quest_id).rules  # noqa: SLF001

        guest_return = self._stat(guest, TOWN_RULES)

        self.assertEqual([message.command for message in guest_return], [commands.CMD_RESULT_WRAPPER])
        self.assertEqual(self.engine._rooms.get(quest_id).rules, quest_rules)  # noqa: SLF001

    def _posted(self, marker: int, town_room: int) -> bytes:
        """A profile posting a quest (party state 1) in the Town room `town_room`."""

        profile = bytearray(self._profile(marker))
        profile[self.layout.party_state_offset] = 1
        offset = self.layout.town_room_offset
        profile[offset:offset + 4] = town_room.to_bytes(4, 'little')
        return bytes(profile)

    def _town_room(self, profile: bytes) -> int:
        return int.from_bytes(profile[self.layout.town_room_offset:self.layout.town_room_offset + 4], 'little')

    def test_returned_and_staying_hunters_see_each_others_quests(self) -> None:
        # Live capture: Town room 1, quest room 2. Back from the quest the client
        # posts with Town id 2 (its quest room id), a hunter who joined room 1
        # posts with 1, and each shows only quests of its own room id (`0x0060edd0`).
        town_id = self._town_with_residents()
        host, guest, newcomer = self.players
        quest_id = self._depart(host, guest, town_id)
        self._join(newcomer, town_id)
        self._stat(guest, TOWN_RULES)
        self._stat(host, TOWN_RULES)

        posted = self._publish(newcomer, self._posted(3, town_id))
        updates = {message.endpoint: message.payload[8:] for message in posted
                   if message.command == commands.CMD_CHANGE_USER_PROPERTY}
        self.assertEqual({self._town_room(updates[host[0]]), self._town_room(updates[guest[0]])}, {quest_id})

        posted = self._publish(host, self._posted(1, quest_id))
        updates = {message.endpoint: message.payload[8:] for message in posted
                   if message.command == commands.CMD_CHANGE_USER_PROPERTY}
        self.assertEqual(self._town_room(updates[newcomer[0]]), town_id)
        self.assertEqual(self._town_room(updates[guest[0]]), quest_id)

        # Rosters carry the id each requester knows its room by.
        record_size = 24 + self.layout.size
        for requester, own_id in ((newcomer, town_id), (guest, quest_id)):
            reply = self._send(requester, commands.CMD_QUERY_USER, struct.pack('>L', own_id))[0]
            count = struct.unpack_from('>L', reply.payload, 4)[0]
            records = [reply.payload[12 + index * record_size:12 + (index + 1) * record_size] for index in range(count)]
            towns = {struct.unpack_from('>L', record, 16)[0]: self._town_room(record[24:]) for record in records}
            self.assertEqual((towns[host[1]], towns[newcomer[1]]), (own_id, own_id))

    def test_started_quest_members_get_no_lobby_callbacks(self) -> None:
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        self._depart(host, guest, town_id)

        messages = self._publish(guest, self._profile(9))

        # Its quest room runs `game.bin`, its Town dropped it at its leave, and the
        # keepalive result is held back in a started quest: only a bare ACK.
        self.assertEqual([(message.endpoint, message.command) for message in messages], [(guest[0], commands.CMD_ACK)])

    def test_lobby_keepalive_in_flight_at_quest_start_gets_no_callback_reply(self) -> None:
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        # In the Town the keepalive echo is mirrored.
        echo = bytes(range(64))
        self.assertEqual(
            [(message.command, message.payload) for message in self._send(host, commands.CMD_SEND_ECHO, echo)],
            [(commands.CMD_SEND_ECHO, echo)],
        )
        self._depart(host, guest, town_id)

        # The client now runs game.bin: no echo reply and no property result,
        # only a bare ACK that takes no reliable sequence.
        self.assertEqual(self._send(host, commands.CMD_SEND_ECHO, echo, type_flags=FLAG_ROOM), [])
        before = self.engine._sessions.get(host[1]).request_number  # noqa: SLF001
        replies = self._publish(host, self._profile(5))
        self.assertEqual([(message.command, message.sequence_number) for message in replies], [(commands.CMD_ACK, 0)])
        self.assertEqual(self.plugin._players[host[1]].profile, self._profile(5))  # noqa: SLF001

        # After the return STAT the lobby is back and keepalives are answered;
        # its result continues the reliable numbering without a gap.
        back = self._stat(host, TOWN_RULES)
        self.assertEqual(back[0].sequence_number, before)
        self.assertEqual(
            [message.command for message in self._send(host, commands.CMD_SEND_ECHO, echo, type_flags=FLAG_ROOM)],
            [commands.CMD_SEND_ECHO],
        )

    def test_abandoning_a_started_quest_unlists_the_hunter(self) -> None:
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        self._depart(host, guest, town_id)

        self.assertEqual(self._town_row_members(waiting, town_id), 3)

        messages = self._send(guest, commands.CMD_LEAVE)

        # Its Town members already dropped it when it left for the quest.
        self.assertEqual([message for message in messages if message.command == commands.CMD_LEAVE], [])
        self.assertEqual(self._town_row_members(waiting, town_id), 2)

    def test_full_town_roster_is_paged_to_fit_the_client_receive_buffer(self) -> None:
        # Live capture: a 6-member roster (996-byte payload, 1016-byte datagram) is
        # dropped by the client's 896-byte receive buffer (`0x001ff4f8`), which
        # resends its request forever ("Moving to Town"). A town of the configured
        # 8 must arrive as pages the roster callback appends (`0x006182f0`).
        create_accounts(self.config, *(f'hunter{index}' for index in range(4, 8)))
        members = self.players + [self._login(f'hunter{index}', f'10.0.1.{index}') for index in range(3, 8)]
        for index, player in enumerate(members):
            self._publish(player, self._profile(index + 1))
        town_id = self._create(members[0], TOWN_RULES, max_players=8)
        for player in members[1:]:
            self._join(player, town_id)

        pages = self._send(members[0], commands.CMD_QUERY_USER, struct.pack('>L', town_id))

        record_size = 24 + self.layout.size
        self.assertGreater(len(pages), 1)
        sids, sequences = [], []
        for number, page in enumerate(pages):
            self.assertEqual(page.command, commands.CMD_QUERY_USER)
            self.assertLessEqual(len(encode_messages([page])), 896)
            room, total, count = struct.unpack_from('>3L', page.payload)
            self.assertEqual((room, total, len(page.payload)), (town_id, 8, 12 + count * record_size))
            last = number == len(pages) - 1
            # Only the last page keeps the room flag; the others carry a packet number.
            self.assertEqual(page.type_flags, (FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE) if last else FLAG_RESPONSE | FLAG_RELIABLE)
            if not last:
                self.assertNotEqual(page.packet_number, 0)
            sids += [struct.unpack_from('>L', page.payload, 12 + index * record_size + 16)[0] for index in range(count)]
            sequences.append(page.sequence_number)
        self.assertEqual(sids, sorted(player[1] for player in members))
        self.assertEqual(sequences, sorted(sequences))

    def test_five_member_roster_is_still_one_reply(self) -> None:
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)

        replies = self._send(host, commands.CMD_QUERY_USER, struct.pack('>L', town_id))

        self.assertEqual([(reply.type_flags, struct.unpack_from('>3L', reply.payload)[1:]) for reply in replies],
                         [(FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE, (3, 3))])

    def _next_sequence(self, player) -> int:
        self.sequences[player[1]] = self.sequences.get(player[1], 0) + 1
        return self.sequences[player[1]]

    def test_reliable_request_resent_after_a_newer_one_is_still_handled_in_order(self) -> None:
        # Live capture (issue 4): the guest's departure reply (selector 4, seq 2242)
        # was lost twice while its next request (2244) got through; the resend of
        # 2242 was then taken for a duplicate and never reached the host, whose
        # departure wait timed out. The client's receive window holds early
        # packets and hands them on in order (`0x001fe778`).
        self._town_with_residents()
        host, guest, _ = self.players
        reply = struct.pack('>2L', 1, host[1]) + bytes((0, 0, 4, 0, 0, 0, 0, 0)) + bytes(768)
        lost, held, later = (self._next_sequence(guest) for _ in range(3))

        early = self._send(guest, commands.CMD_CHANGE_USER_PROPERTY, self._profile(7), sequence=later)
        self.assertEqual([message.command for message in early], [commands.CMD_ACK])
        resent = self._send(guest, commands.CMD_SEND_TARGET, reply, sequence=lost)
        self.assertEqual([message.endpoint for message in resent if message.command == commands.CMD_SEND_TARGET],
                         [host[0]])
        self.assertNotIn(commands.CMD_RESULT_WRAPPER, [message.command for message in resent])
        filled = self._send(guest, commands.CMD_CHANGE_USER_PROPERTY, self._profile(6), sequence=held)
        results = [message for message in filled if message.command == commands.CMD_RESULT_WRAPPER]
        self.assertEqual([message.acknowledge_number for message in results], [held, later])
        self.assertEqual(self.plugin._players[guest[1]].profile, self._profile(7))  # noqa: SLF001

    def test_party_names_reach_the_guest_in_order_when_one_is_resent(self) -> None:
        # Live captures (issues 2 and 3): the host announced N party members but
        # one selector-6 name never reached the guests, leaving a quest slot with
        # a profile and no player (the "clone"). Its resend was dropped by the
        # server as an old sequence.
        self._town_with_residents()
        host, guest, _ = self.players
        names = [
            struct.pack('>2L', 1, guest[1]) + bytes((0, 0, 6, 0, 0, 0, 0, 0)) + bytes((slot, 0)) + bytes(32)
            for slot in range(3)
        ]
        sequences = [self._next_sequence(host) for _ in names]

        relayed = []
        for index in (0, 2, 1):
            relayed += [message.payload[16] for message in
                        self._send(host, commands.CMD_SEND_TARGET, names[index], sequence=sequences[index])
                        if message.command == commands.CMD_SEND_TARGET]

        self.assertEqual(relayed, [0, 1, 2])

    def test_retried_request_is_acked_not_answered_again(self) -> None:
        # Live capture (issue 5): the status request was resent at 200 ms, the
        # server ran it again and sent a second result under a new sequence, and
        # the client took it as the result of its next request (the Town create,
        # whose room id was still -1). Results are routed by selector into fixed
        # callbacks (`0x00202e10`), so a retry is only ACKed: the answer is
        # reliable and resent by the transport until ACKed.
        host = self.players[0]
        status = struct.pack('>L', 0x97200001)
        sequence = self._next_sequence(host)
        first = self._send(host, commands.CMD_CHANGE_USER_STATUS, status, sequence=sequence)
        retry = self._send(host, commands.CMD_CHANGE_USER_STATUS, status, sequence=sequence)

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].type_flags & FLAG_RELIABLE, FLAG_RELIABLE)
        self.assertEqual([message.command for message in retry], [commands.CMD_ACK])
        self.assertEqual(retry[0].sequence_number, 0)

        created = self._create(host, TOWN_RULES)
        self.assertGreater(created, 0)

    def test_a_resent_held_packet_is_handled_once(self) -> None:
        host = self.players[0]
        self._publish(host, self._profile(1))
        missing, held = self._next_sequence(host), self._next_sequence(host)
        for _ in range(2):
            early = self._send(host, commands.CMD_CHANGE_USER_PROPERTY, self._profile(4), sequence=held)
            self.assertEqual([message.command for message in early], [commands.CMD_ACK])

        filled = self._send(host, commands.CMD_CHANGE_USER_PROPERTY, self._profile(3), sequence=missing)

        results = [message.acknowledge_number for message in filled if message.command == commands.CMD_RESULT_WRAPPER]
        self.assertEqual(results, [missing, held])

    def test_kics_restarts_reliable_numbering_in_both_directions(self) -> None:
        # The client zeroes its expected and its own outgoing reliable sequence
        # when it sends the KICS login (MH `0x001fce6c`); its unreliable
        # numbering goes on.
        endpoint, session_id = player = self.players[0]
        self._publish(player, self._profile(1))
        self._publish(player, self._profile(2))
        session = self.engine._sessions.get(session_id)  # noqa: SLF001
        unreliable_before = session.sequence_number

        self._send(player, commands.CMD_LOGIN_TO_KICS, bytes(0x130), FLAG_CHANNEL_BITS)
        replies = self._send(player, commands.CMD_CHANGE_USER_PROPERTY, self._profile(3), sequence=0)

        self.assertEqual([reply.command for reply in replies], [commands.CMD_RESULT_WRAPPER])
        self.assertEqual(replies[0].type_flags & FLAG_RELIABLE, FLAG_RELIABLE)
        self.assertEqual(replies[0].sequence_number, 0)
        self.assertEqual(self.plugin._players[session_id].profile, self._profile(3))  # noqa: SLF001
        self.assertGreater(session.sequence_number, unreliable_before)

    def test_reliable_request_from_an_unknown_session_is_not_acknowledged(self) -> None:
        # A stale client (for example after a server restart) gets no ACK, so it
        # reaches its retry limit and logs in again instead of waiting forever.
        stranger = (Endpoint(host='10.0.9.9', port=5000), 0x0BADF00D)

        replies = self._send(stranger, commands.CMD_CHANGE_USER_STATUS, struct.pack('>L', 0x97200001), sequence=5)

        self.assertEqual(replies, [])

    def test_a_gap_is_never_skipped(self) -> None:
        # The client's window never skips a missing sequence (AM
        # `kkReceiveExtentCheck`), and MH resends for 30 s and more: a late
        # departure selector handed on after later ones fails the guest's quest
        # join (`Chaoz and Ike recieved an error ...pcapng`, selector 5 after 8).
        now = [1000.0]
        self.engine._clock = lambda: now[0]  # noqa: SLF001
        host = self.players[0]
        self._publish(host, self._profile(1))
        lost = self._next_sequence(host)
        later = self._next_sequence(host)

        self.assertEqual([message.command for message in
                          self._send(host, commands.CMD_CHANGE_USER_PROPERTY, self._profile(8), sequence=later)],
                         [commands.CMD_ACK])
        now[0] += 30.0
        self.assertEqual([message for message in self.engine.tick()
                          if message.command == commands.CMD_RESULT_WRAPPER], [])

        handled = self._send(host, commands.CMD_CHANGE_USER_PROPERTY, self._profile(5), sequence=lost)

        self.assertEqual([message.acknowledge_number for message in handled
                          if message.command == commands.CMD_RESULT_WRAPPER], [lost, later])
        self.assertEqual(self.plugin._players[host[1]].profile, self._profile(8))  # noqa: SLF001

    def test_town_roster_names_match_each_login_field(self) -> None:
        # A solo Town creator is its Town's master only if the roster spells its
        # name exactly as its client ID, the login field (`0x005ed948` vs `0x003c6350`).
        host = self.players[0]
        self._publish(host, self._profile(1))
        town_id = self._create(host, TOWN_RULES)

        roster = self._send(host, commands.CMD_QUERY_USER, struct.pack('>L', town_id))[0]

        self.assertEqual(struct.unpack_from('>3L', roster.payload), (town_id, 1, 1))
        self.assertEqual(roster.payload[12:28], pack16('hunter0'))

    def test_join_callback_carries_member_profile(self) -> None:
        host, guest, _ = self.players
        self._publish(host, self._profile(1))
        self._publish(guest, self._profile(2))
        town_id = self._create(host, TOWN_RULES)

        messages = self._join(guest, town_id)

        callback = [message for message in messages if message.command == commands.CMD_JOIN][0]
        self.assertEqual(callback.endpoint, host[0])
        # Reliable: a lost join callback would leave the hunter unseen (`0x00619430`).
        self.assertEqual(callback.type_flags, FLAG_ROOM | FLAG_RESPONSE | FLAG_RELIABLE)
        self.assertEqual(callback.payload[:16], pack16('hunter1'))
        self.assertEqual(struct.unpack_from('>2L', callback.payload, 16), (guest[1], self.layout.size))
        self.assertEqual(callback.payload[24:], self._profile(2))

    def test_exact_reliable_create_retry_creates_nothing_again(self) -> None:
        host = self.players[0]
        payload = bytearray(0x2C)
        payload[0x28:0x2C] = struct.pack('>L', TOWN_RULES)

        sequence = self.sequences.get(host[1], 0) + 1
        self.sequences[host[1]] = sequence
        first = self._send(host, commands.CMD_CREATE_GAME_ROOM, bytes(payload), sequence=sequence)
        retry = self._send(host, commands.CMD_CREATE_GAME_ROOM, bytes(payload), sequence=sequence)

        self.assertEqual(len([message for message in first if message.command == commands.CMD_RESULT_WRAPPER]), 1)
        # The retry is only ACKed: no second Town, no announcement.
        self.assertEqual([message.command for message in retry], [commands.CMD_ACK])
        rooms = self._send(host, commands.CMD_QUERY_GAME_ROOMS, struct.pack('>L', AREA_ID), FLAG_CHANNEL_BITS)
        self.assertEqual(struct.unpack_from('>L', rooms[0].payload, 8)[0], 1)

    def test_exact_join_retry_is_acked_only(self) -> None:
        host, guest, _ = self.players
        self._publish(host, self._profile(1))
        self._publish(guest, self._profile(2))
        town_id = self._create(host, TOWN_RULES)

        sequence = self.sequences.get(guest[1], 0) + 1
        self.sequences[guest[1]] = sequence
        first = self._send(guest, commands.CMD_JOIN, struct.pack('>L', town_id), sequence=sequence)
        retry = self._send(guest, commands.CMD_JOIN, struct.pack('>L', town_id), sequence=sequence)

        self.assertTrue([m for m in first if m.command == commands.CMD_JOIN and m.endpoint == host[0]])
        # A second join callback would count the member twice (`0x00619430`), so
        # the host gets nothing; the guest gets a bare ACK only.
        self.assertEqual([(m.endpoint, m.command) for m in retry], [(guest[0], commands.CMD_ACK)])

    def test_town_directory_events_reach_the_area(self) -> None:
        host, guest, other = self.players
        payload = bytearray(0x2C)
        payload[0:4] = b'Town'
        payload[0x10:0x14] = struct.pack('>L', 4)
        payload[0x28:0x2C] = struct.pack('>L', TOWN_RULES)
        created = self._send(host, commands.CMD_CREATE_GAME_ROOM, bytes(payload))
        town_id = struct.unpack('>2L', [m for m in created if m.command == commands.CMD_RESULT_WRAPPER][0].payload)[1]

        def rows(messages):
            return {
                m.endpoint: struct.unpack_from('>5L', m.payload, 16)
                for m in messages if m.command == commands.CMD_CREATE_GAME_ROOM
            }

        # Slot 4 row: name16, members, 0, rules, max players, room id, to every hunter of the Area.
        self.assertEqual(rows(created), {p[0]: (1, 0, TOWN_RULES, 4, town_id) for p in self.players})
        self.assertEqual(rows(self._join(guest, town_id))[other[0]], (2, 0, TOWN_RULES, 4, town_id))
        # Quest rooms are not listed by the client, so they are not announced.
        quest = bytearray(payload)
        quest[0x28:0x2C] = struct.pack('>L', QUEST_RULES)
        self.assertEqual(rows(self._send(other, commands.CMD_CREATE_GAME_ROOM, bytes(quest))), {})

        self._send(guest, commands.CMD_LEAVE, b'')
        removed = self._send(host, commands.CMD_LEAVE, b'')
        deletes = [m for m in removed if m.command == commands.CMD_DELETE]
        # Slot 5: room id on the room channel (the lobby flag would delete a lobby).
        self.assertEqual({m.payload for m in deletes}, {struct.pack('>L', town_id)})
        self.assertTrue(all(m.type_flags & FLAG_CHANNEL_BITS == FLAG_ROOM for m in deletes))

    def test_area_population_and_roster(self) -> None:
        for index, player in enumerate(self.players):
            self._publish(player, self._profile(index + 1))
        self._send(self.players[2], commands.CMD_LEAVE, b'', FLAG_CHANNEL_BITS)

        area = self._send(self.players[0], commands.CMD_QUERY_AREA, area_query('RED'), FLAG_CHANNEL_BITS)[0]
        # Default Red Land: two Areas, the first holding the two remaining hunters.
        self.assertEqual(struct.unpack_from('>3L', area.payload), (26, 1, 2))
        self.assertEqual(area.payload[12:17], b'RED01')
        self.assertEqual(
            struct.unpack_from('>5L', area.payload, 12 + 16),
            (2, self.plugin.directory.area_capacity, 0, 0, AREA_ID),
        )

        roster = self._send(self.players[0], commands.CMD_QUERY_USER, struct.pack('>L', AREA_ID), FLAG_CHANNEL_BITS)[0]
        self.assertEqual(roster.type_flags, FLAG_CHANNEL_BITS | 0x4000)
        # The searcher is left out of its own Area roster (the hunter search).
        self.assertEqual(struct.unpack_from('>3L', roster.payload), (AREA_ID, 1, 1))
        self.assertEqual(roster.payload[12:28], pack16('hunter1'))

    def test_friend_search_reports_area_and_room(self) -> None:
        town_id = self._town_with_residents()
        searcher, friend, _ = self.players

        reply = self._send(searcher, commands.CMD_SEARCH_USERS_BY_NAME, pack16('hunter1-') + struct.pack('>2L', 8, 1),
                           FLAG_CHANNEL_BITS | FLAG_RELIABLE)[0]

        self.assertEqual(struct.unpack_from('>3L', reply.payload), (0, 1, 1))
        record = reply.payload[12:]
        self.assertEqual(record[:16], pack16('hunter1'))
        self.assertEqual(struct.unpack_from('>4L', record, 16), (1, AREA_ID, town_id, self.layout.size))
        self.assertEqual(record[32:], self._profile(2))

        missing = self._send(searcher, commands.CMD_SEARCH_USERS_BY_NAME, pack16('nobody-') + struct.pack('>2L', 7, 1),
                             FLAG_CHANNEL_BITS | FLAG_RELIABLE)[0]
        self.assertEqual(missing.payload, struct.pack('>3L', 0, 0, 0))

        itself = self._send(searcher, commands.CMD_SEARCH_USERS_BY_NAME, pack16('hunter0-') + struct.pack('>2L', 8, 1),
                            FLAG_CHANNEL_BITS | FLAG_RELIABLE)[0]
        self.assertEqual(itself.payload, struct.pack('>3L', 0, 0, 0))

    def test_friend_status_area_lookup_by_oid(self) -> None:
        # Live request: limit 1, `OID` equal to the Friend's Area id.
        request = struct.pack('>LB3x', 1, 1) + b'OID\x00\x01' + struct.pack('>L', AREA_ID)

        reply = self._send(self.players[0], commands.CMD_QUERY_AREA, request, FLAG_CHANNEL_BITS | FLAG_RELIABLE)[0]

        self.assertEqual(struct.unpack_from('>3L', reply.payload), (1, 1, 1))
        name = reply.payload[12:28].split(b'\x00')[0]
        self.assertEqual(struct.unpack_from('>L', reply.payload, 12 + 32)[0], AREA_ID)
        if self.plugin.profile.status_area_name_skips_prefix:
            # The release prints `name[4:-2]` ("Red Land", "Red Area A").
            self.assertEqual(name[4:-2], b'Red')
        else:
            # The beta looks `name[:-2]` up among the Land keys.
            self.assertEqual(name, b'RED01')
        self.assertEqual(name[-2:], b'01')

        unknown = struct.pack('>LB3x', 1, 1) + b'OID\x00\x01' + struct.pack('>L', 999)
        reply = self._send(self.players[0], commands.CMD_QUERY_AREA, unknown, FLAG_CHANNEL_BITS | FLAG_RELIABLE)[0]
        self.assertEqual(reply.payload, struct.pack('>3L', 1, 1, 0))

    def test_friend_status_area_name_fits_its_field(self) -> None:
        lands = [{'key': 'K', 'name': 'A' * 15, 'areas': 26}]
        with patch.dict('os.environ', {self.plugin.profile.worlds_environment_key: json.dumps({'W': {'lands': lands}})}):
            self.plugin._directory = read_directory(  # noqa: SLF001
                max_players_per_town=8, environment_key=self.plugin.profile.worlds_environment_key
            )
        for area in self.plugin.directory.areas:
            name = self.plugin._status_area_name(area)  # noqa: SLF001
            # 15 characters keep the 16-byte field terminated; the release needs at least 6.
            self.assertLessEqual(len(name), 15)
            self.assertGreaterEqual(len(name), 6 if self.plugin.profile.status_area_name_skips_prefix else 3)
            self.assertEqual(name[-2:], area.name[-2:])

    def test_unknown_area_join_is_rejected(self) -> None:
        reply = self._send(self.players[0], commands.CMD_JOIN, struct.pack('>L', 99), FLAG_CHANNEL_BITS)[0]
        self.assertEqual(reply.command, commands.CMD_RESULT_ERROR)
        self.assertEqual(struct.unpack('>L', reply.payload[:4])[0], commands.CMD_JOIN)

    def test_missing_room_join_is_an_error_result(self) -> None:
        reply = self._join(self.players[0], 0x7777)[-1]
        self.assertEqual(reply.command, commands.CMD_RESULT_ERROR)
        self.assertEqual(reply.type_flags & FLAG_CHANNEL_BITS, FLAG_ROOM)

    def test_area_join_is_refused_when_the_land_is_full(self) -> None:
        lands = [{'key': 'RED', 'areas': 2, 'capacity': 2}]
        with patch.dict('os.environ', {'OPENSNAP_MH_WORLDS': json.dumps({'W': {'lands': lands}})}):
            self.plugin._directory = read_directory(max_players_per_town=8)
        self._send(self.players[2], commands.CMD_LEAVE, b'', FLAG_CHANNEL_BITS)

        refused = self._send(self.players[2], commands.CMD_JOIN, struct.pack('>L', 2), FLAG_CHANNEL_BITS)[0]
        self.assertEqual(refused.command, commands.CMD_RESULT_ERROR)
        # A hunter already counted in the Land may still move between its Areas.
        moved = self._send(self.players[0], commands.CMD_JOIN, struct.pack('>L', 2), FLAG_CHANNEL_BITS)[0]
        self.assertEqual(moved.command, commands.CMD_RESULT_WRAPPER)
        self.assertEqual(struct.unpack('>2L', moved.payload)[1], 0)

    def test_long_town_list_is_paged_to_fit_the_client_receive_buffer(self) -> None:
        # 25 Towns in one Area (three categories) are a 912-byte list; the Town
        # list callback (`0x006173a0`) counts rows across pages.
        room_ids = [
            self.engine._rooms.create_room(  # noqa: SLF001
                name=f'T{index}', password='', rules=((1 << (index % 3)) << 24) | TOWN_RULES, max_players=8,
                lobby_id=AREA_ID, host_session_id=0x1000 + index,
            ).room_id
            for index in range(25)
        ]

        pages = self._send(self.players[0], commands.CMD_QUERY_GAME_ROOMS, struct.pack('>L', AREA_ID),
                           FLAG_CHANNEL_BITS | FLAG_RELIABLE)

        self.assertEqual([page.type_flags for page in pages],
                         [FLAG_LOBBY | FLAG_RESPONSE | FLAG_RELIABLE, FLAG_CHANNEL_BITS | FLAG_RESPONSE | FLAG_RELIABLE])
        self.assertNotEqual(pages[0].packet_number, 0)
        listed = []
        for page in pages:
            self.assertLessEqual(len(encode_messages([page])), 896)
            count = struct.unpack_from('>L', page.payload, 8)[0]
            listed += [struct.unpack_from('>L', page.payload, 12 + index * 36 + 32)[0] for index in range(count)]
        self.assertEqual(sorted(listed), sorted(room_ids))

    def test_town_category_is_limited_to_21_towns(self) -> None:
        for index in range(21):
            self.engine._rooms.create_room(
                name=f'T{index}', password='', rules=TOWN_RULES, max_players=8, lobby_id=AREA_ID,
                host_session_id=0x1000 + index,
            )
        payload = bytearray(0x2C)
        payload[0x28:0x2C] = struct.pack('>L', TOWN_RULES)
        reply = self._send(self.players[1], commands.CMD_CREATE_GAME_ROOM, bytes(payload))[-1]
        self.assertEqual(reply.command, commands.CMD_RESULT_ERROR)
        self.assertEqual(struct.unpack('>L', reply.payload[:4])[0], commands.CMD_CREATE_GAME_ROOM)
        other_category = (1 << 24) | TOWN_RULES
        self.assertIsNotNone(self.engine._rooms.get(self._create(self.players[1], other_category)))

    def test_room_search_by_oid_returns_rules_and_id(self) -> None:
        town_id = self._town_with_residents()
        request = struct.pack('>LB3x', 1, 1) + b'OID\x00' + b'\x01' + struct.pack('>L', town_id)

        reply = self._send(self.players[0], commands.CMD_SEARCH_ROOMS, request, FLAG_CHANNEL_BITS | FLAG_RELIABLE)[0]

        self.assertEqual(struct.unpack_from('>3L', reply.payload), (0, 1, 1))
        record = reply.payload[12:]
        self.assertEqual(len(record), 40)
        self.assertEqual(struct.unpack_from('>L', record, 28)[0], TOWN_RULES)
        self.assertEqual(struct.unpack_from('>L', record, 36)[0], town_id)

        gone = struct.pack('>LB3x', 1, 1) + b'OID\x00' + b'\x01' + struct.pack('>L', 999)
        reply = self._send(self.players[0], commands.CMD_SEARCH_ROOMS, gone, FLAG_CHANNEL_BITS | FLAG_RELIABLE)[0]
        self.assertEqual(reply.payload, struct.pack('>3L', 0, 0, 0))


def pack16(text: str) -> bytes:
    return text.encode().ljust(16, b'\x00')


def area_query(land_key: str) -> bytes:
    """`0x00612760`: `NAME` at least `<key>01` (0x44) and at most `<key>26` (0x46)."""

    return (
        struct.pack('>LB3x', 26, 2)
        + b'NAME\x44' + pack16(f'{land_key}01')
        + b'NAME\x46' + pack16(f'{land_key}26')
    )


class MonsterHunterNaBetaFlowTests(MonsterHunterFlowTests):
    """The whole release flow with the NA public beta's 216-byte profiles (departure state at `+213/+215`)."""

    plugin_class = MonsterHunterNaBetaPlugin

    def test_silent_sessions_end_after_their_idle_limit(self) -> None:
        # The beta lobby sends no keepalive: only a started quest has a limit.
        now = [1000.0]
        self.engine._clock = lambda: now[0]  # noqa: SLF001
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._depart(host, guest, town_id)

        now[0] += plugin_module.QUEST_IDLE_LIMIT_SECONDS + 1
        self.engine.tick()
        self.assertIsNone(self.engine._sessions.get(host[1]))  # noqa: SLF001
        self.assertIsNotNone(self.engine._sessions.get(waiting[1]))  # noqa: SLF001

    def test_release_sized_profiles_are_not_kept(self) -> None:
        host = self.players[0]
        self._publish(host, bytes(140))
        self.assertIsNone(self.plugin._players[host[1]].profile)  # noqa: SLF001


class DirectoryTests(unittest.TestCase):
    def _directory(self, *worlds: dict) -> Directory:
        environment = {key: value for key, value in os.environ.items() if key != 'OPENSNAP_MH_WORLDS'}
        if worlds:
            environment['OPENSNAP_MH_WORLDS'] = json.dumps({world.pop('name'): world for world in worlds})
        with patch.dict('os.environ', environment, clear=True):
            return read_directory(max_players_per_town=8)

    def test_default_is_brave_world_with_red_green_and_blue_lands(self) -> None:
        directory = self._directory()
        self.assertEqual([world.name for world in directory.worlds], ['Brave World'])
        self.assertEqual(
            [(land.name, land.color, land.capacity) for land in directory.lands],
            [('Red', 0xFF5D5D, 750), ('Green', 0x73CB8D, 750), ('Blue', 0x96B5FD, 750)],
        )
        self.assertEqual(directory.areas[0].name, 'RED01')

    def test_worlds_lands_and_areas_come_from_configuration(self) -> None:
        directory = self._directory(
            {'name': 'One', 'lands': [{'key': 'AAA', 'name': 'First', 'areas': 3}, {'key': 'BBB', 'areas': 2}]},
            # Hosted elsewhere: listed only, so its Land keys may repeat ours.
            {'name': 'Two', 'host': 'two.example', 'lands': [{'key': 'AAA', 'areas': 2, 'capacity': 100}]},
        )
        self.assertEqual(directory.local_world.name, 'One')
        self.assertEqual([area.name for area in directory.areas], ['AAA01', 'AAA02', 'AAA03', 'BBB01', 'BBB02'])
        self.assertEqual([area.area_id for area in directory.areas], [1, 2, 3, 4, 5])
        self.assertEqual([land.number for land in directory.lands], [1, 2])
        self.assertEqual([area.name for area in directory.areas_in_name_range('BBB01', 'BBB26')], ['BBB01', 'BBB02'])
        # 3 categories x 21 Towns x 8 players per Area.
        self.assertEqual(directory.area_capacity, 504)
        self.assertEqual([land.capacity for land in directory.lands], [750, 750])
        self.assertEqual(directory.worlds[1].lands[0].capacity, 100)
        self.assertEqual(directory.world_by_host('192.0.2.1', '192.0.2.1').name, 'One')
        self.assertEqual(directory.world_by_host('two.example', '192.0.2.1').name, 'Two')

    def test_invalid_configuration_is_rejected(self) -> None:
        cases = (
            [{'name': 'W', 'lands': []}],
            [{'name': '', 'lands': [{'key': 'A'}]}],
            [{'name': 'W', 'lands': [{'key': ''}]}],
            [{'name': 'W', 'lands': [{'key': 'A', 'areas': 27}]}],
            [{'name': 'W', 'lands': [{'key': 'A', 'capacity': 0}]}],
            [{'name': 'W', 'lands': [{'key': 'A', 'capacity': 0x10000}]}],
            [{'name': 'W', 'lands': [{'key': 'A'}, {'key': 'A'}]}],
            [{'name': 'W', 'lands': [{'key': 'A'}]}, {'name': 'V', 'lands': [{'key': 'B'}]}],
            [{'name': 'W', 'host': 'w.example', 'lands': [{'key': 'A'}]}],
            [{'name': 'W', 'lands': [{'key': 'A'}]}, {'name': 'V', 'host': 'v', 'lands': [{'key': 'A'}]},
             {'name': 'U', 'host': 'v', 'lands': [{'key': 'A'}]}],
            [{'name': f'W{index}', 'host': f'h{index}' if index else '', 'lands': [{'key': 'A'}]} for index in range(17)],
            [{'name': 'W', 'lands': [{'key': 'X' * 14}]}],
            [{'name': 'W', 'lands': [{'key': f'L{index}'} for index in range(57)]}],
            [{'name': 'W', 'lands': [{'key': 'A', 'color': '123456'}]}],
            [{'name': 'W', 'lands': [{'key': 'A', 'color': '#12345G'}]}],
            [{'name': 'W' * 16, 'lands': [{'key': 'A'}]}],
        )
        for worlds in cases:
            with self.assertRaises(ValueError, msg=worlds):
                self._directory(*[dict(world) for world in worlds])
        for raw in ('{"W": ', '["W"]', '{"W": {"lands": [{"key": "A"}]},}'):
            with patch.dict('os.environ', {'OPENSNAP_MH_WORLDS': raw}), self.assertRaises(ValueError):
                read_directory(max_players_per_town=8)

    def test_disabled_worlds_are_validated_but_not_listed(self) -> None:
        directory = self._directory(
            {'name': 'Brave World', 'lands': [{'key': 'RED', 'areas': 2}]},
            {'name': 'Sincere World', 'enabled': False, 'host': 'sincere.example', 'lands': [{'key': 'RED'}]},
            {'name': 'Spare', 'enabled': False, 'lands': [{'key': 'RED'}]},
        )
        self.assertEqual([world.name for world in directory.worlds], ['Brave World'])
        self.assertEqual([area.area_id for area in directory.areas], [1, 2])
        for worlds in (
            [{'name': 'W', 'enabled': 'no', 'lands': [{'key': 'A'}]}],
            [{'name': 'W', 'enabled': False, 'lands': [{'key': 'A'}]}],
            [{'name': 'W', 'lands': [{'key': 'A'}]}, {'name': 'V', 'enabled': False, 'lands': [{'key': 'X' * 14}]}],
        ):
            with self.assertRaises(ValueError, msg=worlds):
                self._directory(*worlds)


class SourceSessionCodecTests(unittest.TestCase):
    """Header session override stays outbound-only."""

    def test_source_session_overrides_only_the_header_word(self) -> None:
        message = SnapMessage(
            endpoint=Endpoint('127.0.0.1', 1),
            type_flags=FLAG_ROOM | FLAG_RELIABLE,
            packet_number=0,
            command=commands.CMD_SEND,
            session_id=0x11111111,
            sequence_number=7,
            acknowledge_number=0,
            payload=b'\x00\x00',
            source_session_id=0x22222222,
        )
        decoded = decode_datagram(encode_messages([message]), message.endpoint)[0]
        self.assertEqual(decoded.session_id, 0x22222222)
        self.assertEqual(decoded.sequence_number, 7)
        self.assertIsNone(decoded.source_session_id)


class AppCodecTests(unittest.TestCase):
    def test_field_round_trip(self) -> None:
        encoded = encode_app_field(b'hunter', 0x35)
        self.assertEqual(decode_app_field(encoded + b'tail', 0x35), (b'hunter', len(encoded)))

    def test_frames_are_split_from_a_coalesced_stream(self) -> None:
        buffer = bytearray(app_frame(1, 0x1002, payload=b'\x00\x00') + app_frame(2, 0x1004)[:5])
        frame = pop_app_frame(buffer)
        self.assertEqual(frame_command(frame), 0x1002)
        self.assertEqual(frame_payload(frame), b'\x00\x00')
        self.assertIsNone(pop_app_frame(buffer))

class AppFlowTrackerTests(unittest.TestCase):
    def test_sessions_on_one_host_keep_their_own_phase(self) -> None:
        flows = AppFlowTracker()
        self.assertEqual(flows.claim('10.0.0.1'), (None, AppPhase.APP1))

        flows.arm_world(1, '10.0.0.1')
        flows.arm_world(2, '10.0.0.1')
        self.assertEqual(flows.claim('10.0.0.1'), (1, AppPhase.WORLD))
        flows.advance('10.0.0.1', 1, AppPhase.POSTWORLD)
        flows.release('10.0.0.1', 1)
        self.assertEqual(flows.claim('10.0.0.1'), (2, AppPhase.WORLD))

        flows.arm_world(1, '10.0.0.1')
        flows.release('10.0.0.1', 2)
        self.assertEqual(flows.claim('10.0.0.1'), (1, AppPhase.LAND))

    def test_host_restarts_at_app1_when_its_players_leave(self) -> None:
        flows = AppFlowTracker()
        flows.arm_world(1, '10.0.0.1')
        flows.claim('10.0.0.1')
        flows.release('10.0.0.1', 1)
        self.assertEqual(flows.claim('10.0.0.1'), (None, AppPhase.LAND))

        flows.forget(1)
        self.assertEqual(flows.claim('10.0.0.1'), (None, AppPhase.APP1))


class AppContentTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        self.root = Path(temp_directory.name)

    def test_market_follows_the_daily_rotation(self) -> None:

        start = date(2026, 1, 1)
        days = [
            struct.unpack('>L', MarketState(lambda day=start + timedelta(days=offset): day).native_response())[0]
            for offset in range(12)
        ]
        self.assertEqual(days, [0, 1, 0, 4, 0, 2, 0, 4, 0, 3, 0, 1])

    def _write_events(self, *quest_ids: int) -> dict[int, bytes]:
        events = self.root / 'events'
        events.mkdir()
        contents = {}
        entries = []
        for index, quest_id in enumerate(quest_ids):
            # Quest information at 0x40 with the number at +0x1e, then padding.
            content = struct.pack('<L', 0x40) + bytes(0x40 - 4 + 0x1E) + struct.pack('<H', quest_id) + bytes(1000)
            (events / f'{quest_id}.mib').write_bytes(content)
            contents[quest_id] = content
            entries.append({'index': index, 'quest_id': quest_id, 'title': f'Quest {quest_id}', 'file': f'{quest_id}.mib'})
        (events / 'manifest.json').write_text(json.dumps({'events': entries}))
        return contents

    def test_event_catalog_serves_754_byte_chunks(self) -> None:
        import hashlib

        content = self._write_events(201)[201]
        catalog = EventCatalog(self.root)
        key = f'201-{hashlib.sha256(content).hexdigest()[:24]}'.encode()
        self.assertEqual(catalog.availability(), struct.pack('>L', 1))
        self.assertEqual(catalog.catalog(lambda data: data), b'\x01' + key + struct.pack('>HL', 1, len(content)))
        chunk = catalog.download(struct.pack('>HLH', 0, 754, 754), lambda data: data)
        self.assertEqual(chunk, struct.pack('>HL', 0, 754) + content[754:])
        with self.assertRaises(ValueError):
            catalog.download(struct.pack('>HLH', 0, 10, 754), lambda data: data)

    def test_event_catalog_rotates_daily_in_index_order(self) -> None:
        contents = self._write_events(201, 202, 203)
        start = date(2026, 1, 1)
        served = [
            EventCatalog(self.root, lambda day=start + timedelta(days=offset): day).current().quest_id
            for offset in range(5)
        ]
        self.assertEqual(served, [201, 202, 203, 201, 202])
        catalog = EventCatalog(self.root, lambda: start + timedelta(days=1))
        chunk = catalog.download(struct.pack('>HLH', 0, 0, 754), lambda data: data)
        self.assertEqual(chunk, struct.pack('>HL', 0, 0) + contents[202][:754])

    def test_event_catalog_keys_differ_per_quest(self) -> None:
        self._write_events(201, 202)
        start = date(2026, 1, 1)
        keys = {
            EventCatalog(self.root, lambda day=start + timedelta(days=offset): day).current().catalog_key
            for offset in range(2)
        }
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(len(key) <= 31 for key in keys))

    def test_event_catalog_rejects_a_mismatched_quest_id(self) -> None:
        self._write_events(201)
        manifest = self.root / 'events' / 'manifest.json'
        manifest.write_text(manifest.read_text().replace('"quest_id": 201', '"quest_id": 299'))
        self.assertEqual(EventCatalog(self.root).availability(), bytes(4))

    def test_information_defaults_to_one_empty_page(self) -> None:
        pages = InformationPages(self.root)
        self.assertEqual(pages.listing(lambda data: b'<' + data + b'>'), b'\x01<>' + struct.pack('>HL', 1, 0))
        self.assertEqual(
            pages.download(struct.pack('>HLH', 0, 0, 754), lambda data: b'<' + data + b'>'),
            struct.pack('>HL', 0, 0) + b'<>',
        )
        with self.assertRaises(ValueError):
            pages.download(struct.pack('>HLH', 1, 0, 754), lambda data: data)

    def test_information_pages_come_from_the_data_directory(self) -> None:
        page = b'<BODY>Welcome' + bytes(1000)
        (self.root / 'news.txt').write_bytes(page)
        (self.root / 'information.json').write_text(json.dumps({'title': 'NEWS', 'pages': ['news.txt']}))
        pages = InformationPages(self.root)
        self.assertEqual(pages.listing(lambda data: data), b'\x01NEWS' + struct.pack('>HL', 1, len(page)))
        self.assertEqual(
            pages.download(struct.pack('>HLH', 0, 754, 754), lambda data: data),
            struct.pack('>HL', 0, 754) + page[754:],
        )

    def test_missing_event_manifest_disables_the_catalog(self) -> None:
        catalog = EventCatalog(self.root)
        self.assertEqual(catalog.availability(), bytes(4))
        self.assertEqual(catalog.catalog(lambda data: data), b'\x00')


class AppServiceTests(unittest.TestCase):
    """APP exchanges over a real loopback socket."""

    def setUp(self) -> None:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        self.data_directory = Path(temp_directory.name)
        _copy_patches(self.data_directory)
        connection = SqliteConnection(self.data_directory / 'records.sqlite')
        self.addCleanup(connection.close)
        self.records = SqlRecordStore(connection)
        self.online = SqlOnlinePlayerStore(connection)
        self.sessions = {
            1: Session(session_id=1, user_id=7, username='hunter', endpoint=Endpoint('127.0.0.1', 4000)),
            2: Session(session_id=2, user_id=8, username='other', endpoint=Endpoint('127.0.0.1', 4001)),
        }
        self.flows = AppFlowTracker()
        worlds = {
            'Brave World': {'description': 'First', 'lands': [
                {'key': 'AAA', 'name': 'First', 'areas': 2},
                {'key': 'BBB', 'name': 'Second', 'areas': 3, 'color': '#4080C0'},
            ]},
            'Sincere World': {'host': 'sincere.example', 'lands': [{'key': 'CCC', 'name': 'Third'}]},
        }
        with patch.dict('os.environ', {'OPENSNAP_MH_WORLDS': json.dumps(worlds)}):
            self.directory = read_directory(max_players_per_town=8)
        self.service = AppService(
            config=AppServiceConfig(
                data_directory=self.data_directory,
                profile=MONSTER_HUNTER_NA,
                game_host='192.0.2.10',
            ),
            flows=self.flows,
            directory=self.directory,
            online_players=self.online,
            records=self.records,
        )
        # The NA public beta: its own service, chosen by its build stamp.
        self.beta_service = AppService(
            config=AppServiceConfig(
                data_directory=self.data_directory / 'beta', profile=MONSTER_HUNTER_NA_BETA, game_host='192.0.2.20'
            ),
            flows=AppFlowTracker(),
            directory=self.directory,
            online_players=self.online,
            records=self.records,
        )
        server = CapcomAppServer(
            host='127.0.0.1',
            port=0,
            titles={APP_BUILD_NA: self.service, APP_BUILD_EU: self.service},
            stamped_builds={NA_PUBLIC_BETA_VERSION: self.beta_service},
        )
        server.start()
        self.addCleanup(server.stop)
        self.port = server.address[1]

    def _login(self, session: Session, area_id: int = 0) -> None:
        """Publish a KICS login (and the player's Area) like the game server, and let the tracker see it."""

        self.online.login('monsterhunter', session)
        self.online.set_area(session.session_id, area_id)
        self.flows.sync(self.online.list('monsterhunter'))

    def _connect(self) -> tuple[socket.socket, bytearray]:
        client = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        self.addCleanup(client.close)
        return client, bytearray()

    @staticmethod
    def _receive(client: socket.socket, buffer: bytearray) -> bytes:
        while (frame := pop_app_frame(buffer)) is None:
            data = client.recv(65535)
            if not data:
                raise ConnectionError('server closed the connection')
            buffer.extend(data)
        return frame

    def _open(
        self, client: socket.socket, buffer: bytearray, *, build: int = 2, flags: int = 1, version: bytes | None = None,
        login: bytes = b'hunter',
    ) -> None:
        """Answer the server's opening 1002 like the client build does (`0x002aeb40`).

        EU (build 3) gets its 6001 at once; NA and the TLES test build (build 3 with
        its stamp `0408131008`) run the NA client, which may speak first.
        """

        if version is None:
            # Each build's own stamp (NA `0x0010fe00`, EU `0x0010fec0`).
            version = EU_STOCK_STAMP if build == 3 else NA_STOCK_STAMP
        opening = self._receive(client, buffer)
        self.assertEqual((frame_command(opening), frame_payload(opening)), (0x1002, b'\x00\x00'))
        reply = encode_app_field(login, 2) + bytes([build, flags]) + encode_app_field(version, 2) + bytes(4)
        client.sendall(app_frame(2, 0x1002, sequence=2, payload=reply))
        self._version = version
        if build == 3 and version != TLES_STAMP:
            self._receive_version(client, buffer)

    def _receive_version(self, client: socket.socket, buffer: bytearray) -> None:
        """6001 is a server push (direction 0x10): flag 1 offering the patch built for the client's version, or 0."""

        payload = b'\x00'
        offered = PATCHES.get(self._version)
        if offered is not None:
            field = encode_app_field(offered.name + offered.version, 0)
            payload = b'\x01' + field + struct.pack('>2L', len(offered.blob), 0)
        self.assertEqual(self._receive(client, buffer), app_frame(0x10, 0x6001, payload=payload))

    def _exchange(self, client: socket.socket, buffer: bytearray, command: int, payload: bytes = b'') -> bytes:
        client.sendall(app_frame(1, command, sequence=5, payload=payload))
        reply = self._receive(client, buffer)
        self.assertEqual((frame_command(reply), frame_sequence(reply)), (command, 5))
        return frame_payload(reply)

    def test_world_then_land_progression(self) -> None:
        self._login(self.sessions[1])
        client, buffer = self._connect()
        self._open(client, buffer)
        client.sendall(app_frame(1, 0x6103, sequence=4))
        self.assertEqual(self._receive(client, buffer), app_frame(2, 0x6103, sequence=4, payload=b'\x00\x00'))
        client.sendall(app_frame(1, 0x1004, sequence=5))
        self.assertEqual(self._receive(client, buffer), app_frame(2, 0x1004, sequence=5))
        client.close()

        done = threading.Event()
        for _ in range(100):
            if self.flows.claim('127.0.0.1') == (1, AppPhase.POSTWORLD):
                done.set()
                break
            threading.Event().wait(0.05)
        self.assertTrue(done.is_set())

        self.flows.advance('127.0.0.1', 1, AppPhase.LAND)
        land, buffer = self._connect()
        self._open(land, buffer)
        land.sendall(app_frame(1, 0x6501, sequence=6))
        reply = self._receive(land, buffer)
        self.assertEqual(frame_payload(reply), b'\x01' + socket.inet_aton('192.0.2.10') + bytes(4))
        land.sendall(app_frame(1, 0x6211, sequence=7))
        self.assertEqual(frame_payload(self._receive(land, buffer)), MarketState().native_response())

    def test_world_list_pages_configured_worlds(self) -> None:
        self._login(self.sessions[1])
        self.flows.advance('127.0.0.1', 1, AppPhase.LAND)
        client, buffer = self._connect()
        self._open(client, buffer)

        client.sendall(app_frame(1, 0x6503, sequence=4, payload=struct.pack('>2H', 0, 16)))
        reply = frame_payload(self._receive(client, buffer))
        self.assertEqual(struct.unpack_from('>2HB', reply), (2, 0, 2))
        # Key = World game server host (this server unless configured), name, 8 bytes, description.
        self.assertEqual(
            reply[5:],
            encode_app_field(b'192.0.2.10', 4) + encode_app_field(b'Brave World', 4) + bytes(8)
            + encode_app_field(b'First', 4)
            # Without a description, the World menu prompt.
            + encode_app_field(b'sincere.example', 4) + encode_app_field(b'Sincere World', 4) + bytes(8)
            + encode_app_field(b'Select a world to login to.', 4),
        )

        client.sendall(app_frame(1, 0x6503, sequence=5, payload=struct.pack('>2H', 1, 16)))
        self.assertEqual(struct.unpack_from('>2HB', frame_payload(self._receive(client, buffer))), (2, 1, 1))

    def test_land_list_pages_each_world_and_populations(self) -> None:
        first, second = self.directory.local_world.lands
        self._login(self.sessions[1], first.areas[0].area_id)
        for session_id, area in ((10, second.areas[0]), (11, second.areas[2])):
            self._login(Session(session_id=session_id, user_id=session_id, username=f'p{session_id}',
                                endpoint=Endpoint('10.0.0.9', 4000)), area.area_id)
        self.flows.advance('127.0.0.1', 1, AppPhase.LAND)
        client, buffer = self._connect()
        self._open(client, buffer)

        brave = encode_app_field(b'192.0.2.10', 9)
        client.sendall(app_frame(1, 0x6504, sequence=9, payload=struct.pack('>2H', 1, 8) + brave))
        reply = frame_payload(self._receive(client, buffer))
        self.assertEqual(struct.unpack_from('>2HB', reply), (2, 1, 1))
        expected_entry = (
            encode_app_field(b'BBB', 9) + encode_app_field(b'Second', 9) + bytes(8)
            # Without a description, the Land menu prompt.
            + encode_app_field(b'Select a land to login to.', 9)
            # Area count, Land capacity and panel color.
            + struct.pack('>2HL', 3, 750, 0x4080C0)
        )
        self.assertEqual(reply[5:], expected_entry)

        sincere = encode_app_field(b'sincere.example', 10)
        client.sendall(app_frame(1, 0x6504, sequence=10, payload=struct.pack('>2H', 0, 8) + sincere))
        reply = frame_payload(self._receive(client, buffer))
        self.assertEqual(struct.unpack_from('>2HB', reply), (1, 0, 1))
        self.assertTrue(reply[5:].startswith(encode_app_field(b'CCC', 10)))

        unknown = encode_app_field(b'nowhere', 11)
        client.sendall(app_frame(1, 0x6504, sequence=11, payload=struct.pack('>2H', 0, 8) + unknown))
        self.assertEqual(self._receive(client, buffer)[6], 0xFF)

        request = b'\x02' + encode_app_field(b'AAA', 9) + encode_app_field(b'BBB', 9) + brave
        client.sendall(app_frame(1, 0x6510, sequence=9, payload=request))
        # Populations of the players the game server published in each Land's Areas.
        self.assertEqual(frame_payload(self._receive(client, buffer)), struct.pack('>B2H', 2, 1, 2) + brave)

        # Players of a World hosted elsewhere are not visible here.
        sincere = encode_app_field(b'sincere.example', 12)
        client.sendall(app_frame(1, 0x6510, sequence=12, payload=b'\x01' + encode_app_field(b'CCC', 12) + sincere))
        self.assertEqual(frame_payload(self._receive(client, buffer)), struct.pack('>BH', 1, 0) + sincere)

    def test_world_connection_reaches_connection_timing(self) -> None:
        self._login(self.sessions[1])
        client, buffer = self._connect()
        self._open(client, buffer)
        exchanges = [
            (0x1007, b'', b'\x00'),
            (0x6212, b'', bytes(4)),
            (0x6201, b'', b'\x01' + encode_app_field(b'', 3) + struct.pack('>HL', 1, 0)),
            (0x6202, struct.pack('>HLH', 0, 0, 754), struct.pack('>HL', 0, 0) + encode_app_field(b'', 3)),
            (0x6203, b'', b'\x00'),
            (0x6211, b'', MarketState().native_response()),
            (0x6213, b'', struct.pack('>8H', 20, 300, 300, 5400, 1800, 1800, 1800, 1800)),
        ]
        for command, request, expected in exchanges:
            client.sendall(app_frame(1, command, sequence=3, payload=request))
            self.assertEqual(frame_payload(self._receive(client, buffer)), expected, hex(command))
        client.sendall(app_frame(1, 0x6501, sequence=3))
        self.assertEqual(frame_payload(self._receive(client, buffer)), b'\x01' + socket.inet_aton('192.0.2.10') + bytes(4))

    def test_na_public_beta_is_served_by_its_own_service(self) -> None:
        # Its 6213 holds four words (no lobby keepalive), and its Worlds point at its own server.
        self.online.login('monsterhunter_na_beta', self.sessions[1])
        client, buffer = self._connect()
        self._open(client, buffer, version=NA_PUBLIC_BETA_VERSION)
        client.sendall(app_frame(1, 0x6213, sequence=3))
        self.assertEqual(frame_payload(self._receive(client, buffer)), struct.pack('>4H', 20, 300, 300, 5400))
        client.sendall(app_frame(1, 0x6501, sequence=3))
        self.assertEqual(frame_payload(self._receive(client, buffer)), b'\x01' + socket.inet_aton('192.0.2.20') + bytes(4))

    def test_eu_app1_keeps_client_defaults_and_serves_its_welcome_page(self) -> None:
        page = b'<BODY>' + b'x' * 800 + b'<END>'
        path = self.data_directory / 'files' / '03' / '02' / 'TOP_INFOR.HTM'
        path.parent.mkdir(parents=True)
        path.write_bytes(page)
        client, buffer = self._connect()
        # EU French: flags `language | 0x10`; APP1 gets 6001 flag 0, never the NA patch.
        self._open(client, buffer, build=3, flags=0x12)

        self.assertEqual(
            self._exchange(client, buffer, 0x6110),
            b'\x00\x00' + encode_app_field(b'', 5) + encode_app_field(b'', 5),
        )
        self.assertEqual(self._exchange(client, buffer, 0x6220, b'\x01'), b'\x01' + bytes(4))
        self.assertEqual(self._exchange(client, buffer, 0x6210), bytes(68))

        name = b'03/02/TOP_INFOR.HTM'
        info = self._exchange(client, buffer, 0x6101, bytes(4) + encode_app_field(name, 5))
        self.assertEqual(info, b'\x01' + bytes(4) + encode_app_field(name, 5) + struct.pack('>L', len(page)))
        downloaded = b''
        for offset in (0, 722):
            chunk = self._exchange(client, buffer, 0x6102, encode_app_field(name, 5) + struct.pack('>LH', offset, 722))
            field_size = len(encode_app_field(name, 5))
            self.assertEqual(chunk[:field_size + 4], encode_app_field(name, 5) + struct.pack('>L', offset))
            downloaded += decode_app_field(chunk[field_size + 4:], 5)[0]
        self.assertEqual(downloaded, page)

        for command in (0x6401, 0x6403, 0x6105):
            self.assertEqual(self._exchange(client, buffer, command), b'\x00\x00')
        # It announces its equipment box (type 2); the server asks for the hunter summary instead.
        self.assertEqual(self._exchange(client, buffer, 0x6320, struct.pack('>BL', 2, 304)), b'\x01')
        self.assertEqual(self._exchange(client, buffer, 0x6320, struct.pack('>BL', 1, 20)), b'\x01')
        self.assertEqual(self._exchange(client, buffer, 0x6321, self._hunter_summary(5000)), b'')
        self.assertEqual(self._exchange(client, buffer, 0x1004), b'')
        # APP1 comes before the KICS login: no player to rank yet.
        self.assertEqual(self.records.top('monsterhunter', 'hrp', 10), [])
        # APP1 does not advance any online flow.
        self.assertEqual(self.flows.claim('127.0.0.1'), (None, AppPhase.APP1))

    def test_stock_eu_downloads_the_description_patch(self) -> None:
        client, buffer = self._connect()
        self._open(client, buffer, build=3, flags=0x11)
        name = EU_PATCH.name
        field = encode_app_field(name, 5)
        downloaded = b''
        # The client asks `min(768 - (len(name) + 12), rest)` bytes at a time (EU `0x002a4840`).
        while len(downloaded) < len(EU_PATCH.blob):
            length = min(746, len(EU_PATCH.blob) - len(downloaded))
            chunk = self._exchange(client, buffer, 0x6002, field + struct.pack('>LH', len(downloaded), length))
            self.assertEqual(chunk[:len(field) + 4], field + struct.pack('>L', len(downloaded)))
            downloaded += decode_app_field(chunk[len(field) + 4:], 5)[0]
        self.assertEqual(downloaded, EU_PATCH.blob)
        self.assertEqual(self._exchange(client, buffer, 0x1004), b'')
        self.assertEqual(self.flows.claim('127.0.0.1'), (None, AppPhase.APP1))

    def test_stock_na_downloads_its_patch_then_continues_app1(self) -> None:
        client, buffer = self._connect()
        self._open(client, buffer)
        self._receive_version(client, buffer)
        field = encode_app_field(NA_PATCH.name, 5)
        downloaded = b''
        while len(downloaded) < len(NA_PATCH.blob):
            length = min(746, len(NA_PATCH.blob) - len(downloaded))
            chunk = self._exchange(client, buffer, 0x6002, field + struct.pack('>LH', len(downloaded), length))
            downloaded += decode_app_field(chunk[len(field) + 4:], 5)[0]
        self.assertEqual(downloaded, NA_PATCH.blob)
        # NA goes on with its Information page (state 33) after the last chunk.
        self.assertEqual(self._exchange(client, buffer, 0x6401), b'\x00\x00')

    def test_disabled_patches_are_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _copy_patches(Path(directory), enabled=False)
            self.assertEqual(load_patches(Path(directory)), {})

    def test_patched_na_gets_no_patch(self) -> None:
        client, buffer = self._connect()
        self._open(client, buffer, version=NA_PATCH.name)
        self._receive_version(client, buffer)
        self.assertEqual(self._exchange(client, buffer, 0x6501), b'\x00')

    def test_patched_eu_and_the_demo_get_no_patch(self) -> None:
        for version in (EU_PATCH.name, SLED_STAMP):
            client, buffer = self._connect()
            # `_open` expects 6001 flag 0 for any version but the stock one.
            self._open(client, buffer, build=3, flags=0x11, version=version)
            self.assertEqual(self._exchange(client, buffer, 0x6220, b'\x01'), b'\x01' + bytes(4))

    def test_missing_named_file_is_published_empty(self) -> None:
        client, buffer = self._connect()
        self._open(client, buffer, build=3, flags=0x11)
        name = b'03/01/TOP_INFOR.HTM'
        info = self._exchange(client, buffer, 0x6101, bytes(4) + encode_app_field(name, 5))
        self.assertEqual(info, b'\x01' + bytes(4) + encode_app_field(name, 5) + bytes(4))
        escape = b'../records.sqlite'
        info = self._exchange(client, buffer, 0x6101, bytes(4) + encode_app_field(escape, 5))
        self.assertEqual(info[-4:], bytes(4))

    def test_na_app1_waits_for_6001_then_serves_its_welcome_page(self) -> None:
        page = b'<BODY>openSNAP<END>'
        path = self.data_directory / 'files' / '02' / 'TOP_INFOR.HTM'
        path.parent.mkdir(parents=True)
        path.write_bytes(page)
        # A stale WORLD claim does not matter: the silent client is in APP1 (mode 0).
        self._login(self.sessions[1])
        client, buffer = self._connect()
        self._open(client, buffer)
        self._receive_version(client, buffer)

        name = b'02/TOP_INFOR.HTM'
        info = self._exchange(client, buffer, 0x6101, bytes(4) + encode_app_field(name, 5))
        self.assertEqual(info, b'\x01' + bytes(4) + encode_app_field(name, 5) + struct.pack('>L', len(page)))
        field = encode_app_field(name, 5)
        chunk = self._exchange(client, buffer, 0x6102, field + struct.pack('>LH', 0, 722))
        self.assertEqual(decode_app_field(chunk[len(field) + 4:], 5)[0], page)
        self.assertEqual(self._exchange(client, buffer, 0x6401), b'\x00\x00')
        # Flag 0: APP1 completes instead of loading the Worlds.
        self.assertEqual(self._exchange(client, buffer, 0x6501), b'\x00')
        self.assertEqual(self._exchange(client, buffer, 0x1004), b'')

    def test_unknown_client_build_is_disconnected(self) -> None:
        client, buffer = self._connect()
        self._receive(client, buffer)
        reply = encode_app_field(b'X', 2) + bytes([9, 0]) + encode_app_field(b'', 2) + bytes(4)
        client.sendall(app_frame(2, 0x1002, sequence=2, payload=reply))
        with self.assertRaises(ConnectionError):
            self._receive(client, buffer)

    def test_tles_runs_the_na_flow(self) -> None:
        # Its LAND connection speaks first and gets no 6001, which NA code would take as a patch offer.
        self._login(self.sessions[1])
        self.flows.advance('127.0.0.1', 1, AppPhase.LAND)
        client, buffer = self._connect()
        self._open(client, buffer, build=3, flags=0x11, version=TLES_STAMP)
        self.assertEqual(self._exchange(client, buffer, 0x6306), b'')
        # A silent connection is APP1 and gets the NA 6001 after the wait, like NA.
        client, buffer = self._connect()
        self._open(client, buffer, build=3, flags=0x11, version=TLES_STAMP)
        self._receive_version(client, buffer)
        self.assertEqual(self._exchange(client, buffer, 0x6501), b'\x00')

    def test_tles_quest_records_are_acknowledged_and_stored(self) -> None:
        self._login(self.sessions[1])
        self.flows.advance('127.0.0.1', 1, AppPhase.LAND)
        client, buffer = self._connect()
        self._open(client, buffer, build=3, flags=0x11, version=TLES_STAMP)
        self.assertEqual(self._exchange(client, buffer, 0x6306), b'')
        # Gypceros (kind 3), lifetime total 7; a Rathian kill (kind 0) comes without its pair.
        self.assertEqual(self._exchange(client, buffer, 0x6307, struct.pack('>BBHBL', 1, 5, 0x0105, 3, 7)), b'')
        self.assertEqual(self._exchange(client, buffer, 0x6307, struct.pack('>BBH', 1, 5, 0x0105)), b'')

        self.assertEqual(
            [(record.player, record.score, dict(record.details))
             for record in self.records.top('monsterhunter', 'tles-kills-20', 10)],
            [('hunter', 7, {'quest': 261, 'hunter_rank': 5})],
        )
        self.assertEqual(self.records.boards('monsterhunter'), ['tles-kills-20'])
        # Its resource upload is not traced: nothing is asked for.
        self.assertEqual(self._exchange(client, buffer, 0x6320, struct.pack('>BL', 1, 20)), b'\x00')

    def test_eu_quest_records_are_acknowledged_and_stored(self) -> None:
        self._login(self.sessions[1])
        self.flows.advance('127.0.0.1', 1, AppPhase.LAND)
        client, buffer = self._connect()
        self._open(client, buffer, build=3, flags=0x15)

        counts = [0] * 34
        counts[3], counts[10] = 2, 1
        # Veggie Elder (index 9), Cart (17) and Poogie (31) can't be legally killed: not tracked.
        counts[9], counts[17], counts[31] = 4, 1, 2
        hunts = struct.pack('>BBH', 1, 12, 0x0105) + b''.join(
            struct.pack('>BL', index, count) for index, count in enumerate(counts)
        )
        self.assertEqual(self._exchange(client, buffer, 0x6307, hunts), b'')
        clear = struct.pack('>BBHBL', 2, 4, 0x0105, 0, 754)
        self.assertEqual(self._exchange(client, buffer, 0x6307, clear), b'')
        self.assertEqual(self._exchange(client, buffer, 0x6320, struct.pack('>BL', 1, 20)), b'\x01')
        self.assertEqual(self._exchange(client, buffer, 0x6321, self._hunter_summary(150000)), b'')
        self.assertEqual(
            [(record.player, record.score) for record in self.records.top('monsterhunter', 'hrp', 10)],
            [('hunter', 150000)],
        )

        self.assertEqual(
            [(record.player, record.score, dict(record.details))
             for record in self.records.top('monsterhunter', 'clear-261', 10)],
            [('hunter', 754, {'weapon_class': 4})],
        )
        # Record index k counts monster k + 1: Mosswine (4) and Rathalos (11); the same hunt again adds up.
        self.assertEqual(self._exchange(client, buffer, 0x6307, hunts), b'')
        self.assertEqual([record.score for record in self.records.top('monsterhunter', 'kills-04', 10)], [4])
        self.assertEqual([record.score for record in self.records.top('monsterhunter', 'kills-11', 10)], [2])
        # Veggie Elder (10), Cart (18) and Poogie (32) have no board.
        self.assertEqual(self.records.boards('monsterhunter'), ['clear-261', 'hrp', 'kills-04', 'kills-11'])

    def test_quest_records_go_to_the_login_that_sent_them(self) -> None:
        # Two players behind one address: only the connection's 1002 login tells them apart.
        self._login(self.sessions[1])
        self._login(self.sessions[2])
        clear = struct.pack('>BBHBL', 2, 4, 0x0105, 0, 754)
        for login in (b'other', b'nobody'):
            client, buffer = self._connect()
            self._open(client, buffer, build=3, flags=0x15, login=login)
            self.assertEqual(self._exchange(client, buffer, 0x6307, clear), b'')
            client.close()

        self.assertEqual([record.player for record in self.records.top('monsterhunter', 'clear-261', 10)], ['other'])

    def _download_page(self, name: bytes, *, eu: bool) -> str:
        client, buffer = self._connect()
        # Record menu: APP mode 3 asks for the path after `lbs://lbs/`.
        if eu:
            self._open(client, buffer, build=3, flags=0x14)
        else:
            self._open(client, buffer)
        info = self._exchange(client, buffer, 0x6101, bytes(4) + encode_app_field(name, 5))
        size = struct.unpack('>L', info[-4:])[0]
        field = encode_app_field(name, 5)
        page = b''
        for offset in range(0, size, 722):
            chunk = self._exchange(client, buffer, 0x6102, field + struct.pack('>LH', offset, 722))
            page += decode_app_field(chunk[len(field) + 4:], 5)[0]
        self.assertEqual(len(page), size)
        return page.decode()

    def _write_quests(self, quests: dict[int, tuple[str, str]]) -> None:
        (self.data_directory / 'quests.json').write_text(json.dumps(
            {str(quest): {'name': name, 'category': category} for quest, (name, category) in quests.items()}
        ))

    @staticmethod
    def _hunter_summary(points: int, *, size: int = 20) -> bytes:
        """6321 type 1 payload: `u8 1, u32 0, u16 20, field(summary)` with HRP at summary +12."""

        summary = bytes(range(1, 13)) + struct.pack('<L', points) + bytes((20, 0, 0, 0))
        return struct.pack('>B4xH', 1, size) + encode_app_field(summary[:size], 5)

    def test_na_quest_return_uploads_the_hunter_rank_points(self) -> None:
        self._login(self.sessions[1])
        self.flows.advance('127.0.0.1', 1, AppPhase.LAND)
        client, buffer = self._connect()
        self._open(client, buffer)
        self.assertEqual(self._exchange(client, buffer, 0x6306, bytes(32)), b'')
        self.assertEqual(self._exchange(client, buffer, 0x6320, struct.pack('>BL', 1, 20)), b'\x01')
        self.assertEqual(self._exchange(client, buffer, 0x6321, self._hunter_summary(9_999_999)), b'')
        # A failed quest lowers it again.
        self.assertEqual(self._exchange(client, buffer, 0x6321, self._hunter_summary(9_999_900)), b'')
        self.assertEqual(
            [(record.player, record.score) for record in self.records.top('monsterhunter', 'hrp', 10)],
            [('hunter', 9_999_900)],
        )

    def test_malformed_hunter_summary_is_acknowledged_and_ignored(self) -> None:
        self._login(self.sessions[1])
        self.flows.advance('127.0.0.1', 1, AppPhase.LAND)
        client, buffer = self._connect()
        self._open(client, buffer)
        for payload in (b'\x01', self._hunter_summary(10_000_000), self._hunter_summary(10, size=16)):
            self.assertEqual(self._exchange(client, buffer, 0x6321, payload), b'')
        self.assertEqual(self.records.top('monsterhunter', 'hrp', 10), [])

    def test_hunter_rank_points_page_ranks_ties_at_the_maximum_by_who_reached_it_first(self) -> None:
        for user_id, player, points in ((1, 'Aiko', 9_999_999), (2, 'Cato', 1200), (3, 'Bren', 9_999_999)):
            self._submit('hrp', Ranking.LATEST, user_id, player, points)
        # Aiko drops below the maximum and climbs back: now behind Bren.
        self._submit('hrp', Ranking.LATEST, 1, 'Aiko', 9_999_000)
        self._submit('hrp', Ranking.LATEST, 1, 'Aiko', 9_999_999)
        text = self._pages().render(b'02/RANK_HRP_1.HTM').decode()
        self.assertLess(text.index('Bren'), text.index('Aiko'))
        self.assertLess(text.index('Aiko'), text.index('Cato'))
        index = self._pages().render(b'02/DATABASE.HTM').decode()
        self.assertIn('<a href="RANK_HRP_1.HTM">Hunter Rank Points</a>', index)

    def test_hunter_rank_points_list_the_top_hundred_on_four_pages(self) -> None:
        for user in range(130):
            self._submit('hrp', Ranking.LATEST, user, f'H{user:03d}', user)
        pages = self._pages()
        second = pages.render(b'02/RANK_HRP_2.HTM').decode()
        # Ranks continue across pages: rank 26 is the 26th highest (HRP 104).
        self.assertIn('<td>26</td>', second)
        self.assertIn('H104', second)
        self.assertIn('href="RANK_HRP_3.HTM"', second)
        self.assertNotIn('H029', pages.render(b'02/RANK_HRP_4.HTM').decode())
        self.assertIsNone(pages.render(b'02/RANK_HRP_5.HTM'))

    def test_viewer_rows_are_yellow_and_added_below_the_top_list(self) -> None:
        for user in range(6):
            self._submit('kills-11', Ranking.TOTAL, user, f'p{user}', 10 - user)
        pages = self._pages()
        own = '<tr>' + ''.join(f'<td><font color="#FFFF00">{cell}</font></td>' for cell in ('5', 'p4', '6')) + '</tr>'
        # Rank 5 is below the top three: listed after them with its own rank.
        text = pages.render(b'02/RANK_MON_1.HTM', 4).decode()
        self.assertLess(text.index('<td>p2</td>'), text.index(own))
        self.assertNotIn('p3', text)
        # Rank 2 is in the top three: highlighted in place, not repeated.
        text = pages.render(b'02/RANK_MON_1.HTM', 1).decode()
        self.assertEqual(text.count('p1'), 1)
        self.assertLess(text.index('<td>p0</td>'), text.index('#FFFF00">p1</font>'))
        self.assertLess(text.index('#FFFF00">p1</font>'), text.index('<td>p2</td>'))
        # No logged-in player, or one without a record: the top three only.
        for viewer in (None, 99):
            self.assertNotIn('#FFFF00', pages.render(b'02/RANK_MON_1.HTM', viewer).decode())

    def test_hunter_rank_points_pages_add_the_viewer_beyond_the_top_hundred(self) -> None:
        for user in range(130):
            self._submit('hrp', Ranking.LATEST, user, f'H{user:03d}', user)
        pages = self._pages()
        for number in range(1, 5):
            text = pages.render(f'02/RANK_HRP_{number}.HTM'.encode(), 0).decode()
            # Last row of the page's table.
            self.assertIn(
                '<tr><td><font color="#FFFF00">130</font></td><td><font color="#FFFF00">H000</font></td>'
                '<td><font color="#FFFF00">0</font></td></tr>\n</table>', text
            )
        # Rank 30 is listed in place on page 2 and added below page 1.
        self.assertEqual(pages.render(b'02/RANK_HRP_2.HTM', 100).decode().count('H100'), 1)
        self.assertIn('<font color="#FFFF00">30</font>', pages.render(b'02/RANK_HRP_1.HTM', 100).decode())

    def test_record_page_download_shows_the_logged_in_players_rows(self) -> None:
        self._login(self.sessions[1])
        self._submit('clear-27', Ranking.LOWEST, 7, 'hunter', 60)
        self.assertIn('#FFFF00">hunter</font>', self._download_page(b'02/RANK_OTHR_1.HTM', eu=False))

    def _submit(self, board: str, ranking: Ranking, user_id: int, player: str, score: int) -> None:
        self.records.submit(
            game='monsterhunter', board=board, ranking=ranking, user_id=user_id, player=player, score=score
        )

    def _pages(self) -> RecordPages:
        return RecordPages(self.records, 'monsterhunter', load_quests(self.data_directory))

    def test_eu_record_index_is_served_from_its_own_directory(self) -> None:
        text = self._download_page(b'03/04/DATABASE.HTM', eu=True)
        self.assertTrue(text.startswith('<html>') and text.endswith('</html>\n'))
        self.assertIn('<body bgcolor="#101820"', text)
        self.assertIn('<a href="RANK_MON_1.HTM">Most monsters hunted</a>', text)
        self.assertIn('<a href="RANK_EVNT_1.HTM">Event quests</a> (0 quests)', text)

    def test_record_index_counts_ranked_quests_per_category(self) -> None:
        self._write_quests({27: ('Slay the Rathian!', 'hunt'), 201: ('Gathering - Forest and Hills', 'event')})
        self._submit('clear-27', Ranking.LOWEST, 7, 'hunter', 60)
        text = self._pages().render(b'03/04/DATABASE.HTM').decode()
        self.assertIn('<a href="RANK_HUNT_1.HTM">Hunt quests</a> (1 quest)', text)
        self.assertIn('<a href="RANK_EVNT_1.HTM">Event quests</a> (0 quests)', text)
        self.assertNotIn('Other quests', text)
        # The openSNAP patch makes NA send records too, so the EU-only note is off.
        self.assertNotIn('only the European release', text)
        self.assertIn('<a href="AMUSA_MENU_BACK">Close</a>', text)

    def test_na_record_pages_are_served_from_02(self) -> None:
        self._submit('clear-261', Ranking.LOWEST, 7, 'hunter', 754)
        text = self._download_page(b'02/DATABASE.HTM', eu=False)
        self.assertIn('<a href="RANK_OTHR_1.HTM">Other quests</a> (1 quest)', text)
        page = self._download_page(b'02/RANK_OTHR_1.HTM', eu=False)
        self.assertIn('Quest 261', page)
        self.assertIn('<tr><td>1</td><td>hunter</td><td>12\'34"</td></tr>', page)

    def test_tles_record_pages_show_its_kill_totals(self) -> None:
        pages = self._pages()
        # TLES opens `lbs://lbs/03/DATABASE.HTM`; the totals page is linked once a total is stored.
        self.assertNotIn('TLES_KILLS.HTM', pages.render(b'03/DATABASE.HTM').decode())
        for user, totals in ((1, (3, 7)), (2, (5,))):
            for total in totals:
                self._submit('tles-kills-20', Ranking.HIGHEST, user, f'p{user}', total)
        self.assertIn('<a href="TLES_KILLS.HTM">', pages.render(b'03/DATABASE.HTM').decode())
        page = pages.render(b'03/TLES_KILLS.HTM').decode()
        self.assertIn('Gypceros', page)
        # Each hunter's latest (highest) total, highest first.
        self.assertLess(page.index('<td>p1</td><td>7</td>'), page.index('<td>p2</td><td>5</td>'))
        self.assertNotIn('<td>3</td>', page)

    def test_category_pages_hold_six_quests_in_quest_order(self) -> None:
        self._write_quests({quest: (f'Hunt {quest}', 'hunt') for quest in range(1, 11)})
        for quest in range(10, 0, -1):
            for user in range(6):
                self._submit(f'clear-{quest}', Ranking.LOWEST, user, f'p{user}', 60 + user)
        pages = self._pages()
        first = pages.render(b'03/01/RANK_HUNT_1.HTM').decode()
        self.assertIn('Page 1 of 2', first)
        self.assertEqual(re.findall(r'#(\d+) Hunt', first), [str(quest) for quest in range(1, 7)])
        # Top 4 per quest.
        self.assertIn('<td>p3</td>', first)
        self.assertNotIn('<td>p4</td>', first)
        self.assertIn('<a href="RANK_HUNT_2.HTM">Next</a>', first)
        self.assertNotIn('Previous', first)
        second = pages.render(b'03/01/RANK_HUNT_2.HTM').decode()
        self.assertEqual(re.findall(r'#(\d+) Hunt', second), ['7', '8', '9', '10'])
        self.assertIn('<a href="RANK_HUNT_1.HTM">Previous</a>', second)
        self.assertNotIn('Next', second)
        self.assertIsNone(pages.render(b'03/01/RANK_HUNT_3.HTM'))

    def test_monster_page_shows_the_top_three_per_monster(self) -> None:
        for user, kills in enumerate((5, 9, 1, 7)):
            self._submit('kills-11', Ranking.TOTAL, user, f'p<{user}>', kills)
        self._submit('kills-11', Ranking.TOTAL, 0, 'p<0>', 6)
        text = self._pages().render(b'02/RANK_MON_1.HTM').decode()
        self.assertIn('Rathalos', text)
        self.assertNotIn('Rathian', text)
        self.assertEqual(
            re.findall(r'<tr><td>(\d)</td><td>([^<]*)</td><td>(\d+)</td></tr>', text),
            [('1', 'p&lt;0&gt;', '11'), ('2', 'p&lt;1&gt;', '9'), ('3', 'p&lt;3&gt;', '7')],
        )
        self.assertIn('<a href="DATABASE.HTM">Index</a>', text)

    def test_monster_pages_hold_eight_monsters_in_id_order(self) -> None:
        # Monsters 1-12 without Veggie Elder (10): 11 tracked monsters.
        for monster in range(1, 13):
            self._submit(f'kills-{monster:02d}', Ranking.TOTAL, 1, 'p', 1)
        pages = self._pages()
        first = pages.render(b'02/RANK_MON_1.HTM').decode()
        self.assertIn('Page 1 of 2', first)
        self.assertEqual(first.count('>Rank</font>'), 8)
        self.assertIn('Rathian', first)
        self.assertIn('<a href="RANK_MON_2.HTM">Next</a>', first)
        second = pages.render(b'02/RANK_MON_2.HTM').decode()
        self.assertIn('Rathalos', second)
        self.assertNotIn('Veggie Elder', first + second)
        self.assertIsNone(pages.render(b'02/RANK_MON_3.HTM'))

    def test_paged_record_pages_link_the_first_last_and_nearby_page_numbers(self) -> None:
        self._write_quests({quest: (f'Quest name {quest}', 'hunt') for quest in range(100, 202)})
        for quest in range(100, 202):
            self._submit(f'clear-{quest}', Ranking.LOWEST, 1, 'p', 60)
        pages = self._pages()

        def numbers(number: int) -> str:
            text = pages.render(f'02/RANK_HUNT_{number}.HTM'.encode()).decode()
            lines = [line for line in text.split('<br>\n') if f'<b>{number}</b>' in line]
            # The title panel's and the bottom bar's.
            self.assertEqual(len(lines), 2)
            return lines[0]

        # 102 quests, 6 per page: 17 pages.
        link = '<a href="RANK_HUNT_{0}.HTM">{0}</a>'.format
        self.assertEqual(numbers(1), ' '.join(['<b>1</b>', link(2), link(3), '...', link(17)]))
        self.assertEqual(
            numbers(9), ' '.join([link(1), '...', link(7), link(8), '<b>9</b>', link(10), link(11), '...', link(17)])
        )
        # A gap of one page shows that page.
        self.assertEqual(numbers(4), ' '.join([link(page) if page != 4 else '<b>4</b>' for page in range(1, 7)] + ['...', link(17)]))
        self.assertEqual(numbers(17), ' '.join([link(1), '...', link(15), link(16), '<b>17</b>']))

    def test_only_record_page_names_are_generated(self) -> None:
        pages = self._pages()
        for name in (b'04/DATABASE.HTM', b'03/4/DATABASE.HTM', b'02/RANK_XXXX_1.HTM', b'02/RANK_HUNT_0.HTM',
                     b'02/RANK_MON.HTM', b'02/TOP_INFOR.HTM'):
            self.assertIsNone(pages.render(name), name)

    def test_record_pages_stay_within_what_the_browser_draws(self) -> None:
        # Longest quest names, 15-character usernames (the signup maximum), every quest and monster ranked,
        # and a viewer (user 5) below every top list, whose own rows are added.
        longest = 'The GMR Heavy Metal Crusade!'
        self._write_quests({quest: (longest, 'hunt') for quest in range(100, 400)})
        for user in range(6):
            player = f'Hunter_name_{user:03d}'
            for quest in range(100, 400):
                self._submit(f'clear-{quest}', Ranking.LOWEST, user, player, 59999 + user // 5)
            for monster in range(1, 35):
                self._submit(f'kills-{monster:02d}', Ranking.TOTAL, user, player, 999999 - user // 5)
        for user in range(150):
            self._submit('hrp', Ranking.LATEST, user, f'Hunter_name_{user:03d}', 9_999_999 - user // 140)
        pages = self._pages()
        for name in (
            b'03/05/DATABASE.HTM', b'03/05/RANK_HUNT_1.HTM', b'03/05/RANK_MON_1.HTM', b'03/05/RANK_MON_4.HTM',
            b'03/05/RANK_HUNT_25.HTM', b'03/05/RANK_MON_2.HTM', b'03/05/RANK_HRP_1.HTM', b'03/05/RANK_HRP_2.HTM',
            b'03/05/RANK_HRP_4.HTM',
        ):
            page = pages.render(name, 140 if b'HRP' in name else 5)
            if b'RANK_' in name:
                self.assertIn(b'#FFFF00">Hunter_', page, name)
            self.assertLessEqual(len(page), RECORD_PAGE_SAFE_SIZE, name)
            self.assertLessEqual(page.count(b'<td'), RECORD_PAGE_SAFE_CELLS, name)

    def test_na_world_connection_is_recognised_without_a_tracked_session(self) -> None:
        # MH logs out of SNAP between KICS and World selection, which resets the
        # host to APP1; the NA client's immediate 1007 still marks a WORLD connection.
        self.assertEqual(self.flows.claim('127.0.0.1'), (None, AppPhase.APP1))
        client, buffer = self._connect()
        self._open(client, buffer)
        self.assertEqual(self._exchange(client, buffer, 0x1007), b'\x00')
        self.assertEqual(self._exchange(client, buffer, 0x6103), b'\x00\x00')
        self.assertEqual(self._exchange(client, buffer, 0x1004), b'')
        client.close()
        for _ in range(100):
            if self.flows.claim('127.0.0.1') == (None, AppPhase.POSTWORLD):
                break
            threading.Event().wait(0.05)
        self.assertEqual(self.flows.claim('127.0.0.1'), (None, AppPhase.POSTWORLD))

    def test_na_online_connection_gets_no_version_push(self) -> None:
        self._login(self.sessions[1])
        client, buffer = self._connect()
        self._open(client, buffer)
        # The next frame is the reply to the client's first request, not a 6001.
        self.assertEqual(self._exchange(client, buffer, 0x1007), b'\x00')

    def _ram_server(self, directory: Directory | None = None) -> CapcomAppServer:
        """A second APP server whose EU service delivers the custom blob in RAM."""

        service = AppService(
            config=AppServiceConfig(
                data_directory=self.data_directory,
                profile=MONSTER_HUNTER_NA,
                game_host='192.0.2.10',
                ram_patch=(DATA_DIRECTORY / 'patches' / 'eu_custom_1002.bin').read_bytes(),
            ),
            flows=AppFlowTracker(),
            directory=directory or self.directory,
            online_players=self.online,
            records=self.records,
        )
        server = CapcomAppServer(
            host='127.0.0.1',
            port=0,
            titles={APP_BUILD_NA: service, APP_BUILD_EU: service},
        )
        server.start()
        self.addCleanup(server.stop)
        return server

    def _ram_client(self, server: CapcomAppServer) -> tuple[socket.socket, bytearray]:
        client = socket.create_connection(('127.0.0.1', server.address[1]), timeout=5)
        self.addCleanup(client.close)
        return client, bytearray()

    def _ram_open(self, client: socket.socket, buffer: bytearray, version: bytes) -> None:
        """Answer 1002; RAM delivery replaces the 6001 patch offer with flag 0."""

        opening = self._receive(client, buffer)
        self.assertEqual((frame_command(opening), frame_payload(opening)), (0x1002, b'\x00\x00'))
        reply = (
            encode_app_field(b'hunter', 2)
            + bytes([APP_BUILD_EU, 0x11])
            + encode_app_field(version, 2)
            + bytes(4)
        )
        client.sendall(app_frame(2, 0x1002, sequence=2, payload=reply))
        self.assertEqual(self._receive(client, buffer), app_frame(0x10, 0x6001, payload=b'\x00'))

    def _ram_world_list(self, client: socket.socket, buffer: bytearray, sequence: int) -> bytes:
        """Open a WORLD connection with 1007, then ask for the first World page."""

        client.sendall(app_frame(1, 0x1007, sequence=sequence))
        opening = self._receive(client, buffer)
        self.assertEqual(
            (frame_command(opening), frame_sequence(opening), frame_payload(opening)),
            (0x1007, sequence, b'\x00'),
        )
        client.sendall(app_frame(1, 0x6503, sequence=sequence + 1, payload=struct.pack('>2H', 0, 16)))
        page = self._receive(client, buffer)
        self.assertEqual((frame_command(page), frame_sequence(page)), (0x6503, sequence + 1))
        return page

    def _ram_final_page(self, client: socket.socket, buffer: bytearray, sequence: int) -> bytes:
        """Ask for the page at start 8, where the final page holds record 16."""

        client.sendall(app_frame(1, 0x6503, sequence=sequence, payload=struct.pack('>2H', 8, 8)))
        page = self._receive(client, buffer)
        self.assertEqual((frame_command(page), frame_sequence(page)), (0x6503, sequence))
        return page

    def test_ram_patch_burst_delivers_the_custom_blob(self) -> None:
        server = self._ram_server()
        client, buffer = self._ram_client(server)
        self._ram_open(client, buffer, EU_STOCK_STAMP)
        page = self._ram_world_list(client, buffer, sequence=3)
        blob = (DATA_DIRECTORY / 'patches' / 'eu_custom_1002.bin').read_bytes()
        worlds = [
            ('192.0.2.10', 'Brave World', 'First'),
            ('sincere.example', 'Sincere World', 'Select a world to login to.'),
        ]
        prelude_plan = build_ram_patch(blob, worlds, sequence=4)
        self.assertEqual(frame_payload(page), prelude_plan.prelude)
        self.assertEqual(struct.unpack_from('>2HB', prelude_plan.prelude), (16, 0, 8))

        # The 8-record page advances the client's cursor; its next request
        # starts at 8 and gets the 9-record page ending in record 16.
        final_page = self._ram_final_page(client, buffer, 5)
        plan = build_ram_patch(blob, worlds, sequence=5)
        self.assertEqual(frame_payload(final_page), plan.reply)
        self.assertEqual(struct.unpack_from('>2HB', plan.reply), (2, 8, 9))
        for expected in plan.frames:
            self.assertEqual(self._receive(client, buffer), expected)

        # The burst leaves requests behind: the first final page's world
        # status, the upload's chunk requests, the payload completion's Market,
        # and the prelude's next World page.  The first 6502 is answered: its
        # reply follows the burst, so the hijacked handler runs the shellcode.
        # The handler page's 6502 is then a duplicate and stays unanswered, as
        # do the chunk and Market requests.  The handler write's Market request
        # is ignored too, and later 6502/6503 requests are normal.
        client.sendall(app_frame(1, 0x6502, sequence=6, payload=b'\x00'))
        reply = self._receive(client, buffer)
        self.assertEqual((frame_command(reply), frame_sequence(reply)), (0x6502, 6))
        self.assertEqual(frame_payload(reply), b'\x01\x00\x00\x00\x01\x00\x00\x00\x00')
        client.sendall(app_frame(1, 0x6204, sequence=7, payload=b'\x00\x01' + bytes(4)))
        client.sendall(app_frame(1, 0x6211, sequence=8))
        client.sendall(app_frame(1, 0x6503, sequence=9, payload=struct.pack('>2H', 8, 8)))
        client.sendall(app_frame(1, 0x6502, sequence=10, payload=b'\x00'))
        client.sendall(app_frame(1, 0x6211, sequence=11))
        client.sendall(app_frame(1, 0x6502, sequence=12, payload=b'\x00'))
        self.assertEqual(frame_sequence(self._receive(client, buffer)), 12)

        # The burst runs once per connection; later World pages are normal.
        client.sendall(app_frame(1, 0x6503, sequence=13, payload=struct.pack('>2H', 0, 16)))
        later = self._receive(client, buffer)
        self.assertEqual((frame_command(later), frame_sequence(later)), (0x6503, 13))
        self.assertEqual(struct.unpack_from('>2HB', frame_payload(later)), (2, 0, 2))

    def test_ram_patch_reaches_an_official_1001_client(self) -> None:
        server = self._ram_server()
        client, buffer = self._ram_client(server)
        self._ram_open(client, buffer, b'0509261742')
        self._ram_world_list(client, buffer, sequence=3)
        page = self._ram_final_page(client, buffer, 5)
        self.assertEqual(struct.unpack_from('>2HB', frame_payload(page)), (2, 8, 9))
        self.assertEqual(frame_command(self._receive(client, buffer)), 0x6204)

    def test_ram_patch_skips_the_demo_a_patched_client_and_na(self) -> None:
        # The demo, an unknown EU build, an already patched client and NA never
        # burst: the World page stays the normal two-record list.
        for version in (SLED_STAMP, BLOB_MAGIC, b'0501010101'):
            server = self._ram_server()
            client, buffer = self._ram_client(server)
            self._ram_open(client, buffer, version)
            page = self._ram_world_list(client, buffer, sequence=3)
            self.assertEqual(struct.unpack_from('>2HB', frame_payload(page)), (2, 0, 2))
            client.sendall(app_frame(1, 0x6503, sequence=5, payload=struct.pack('>2H', 0, 16)))
            later = self._receive(client, buffer)
            self.assertEqual((frame_command(later), frame_sequence(later)), (0x6503, 5))

        # TLES (EU build 3, NA flow) and NA itself answer first and are never
        # offered the EU RAM patch; NA needs `OPENSNAP_MH_RAM_PATCH_NA`.
        for build, version in ((APP_BUILD_EU, TLES_STAMP), (APP_BUILD_NA, NA_STOCK_STAMP)):
            server = self._ram_server()
            client, buffer = self._na_ram_open(server, version=version, build=build)
            page = self._na_ram_world_list(client, buffer)
            self.assertEqual(struct.unpack_from('>2HB', frame_payload(page)), (2, 0, 2))

    def _na_ram_server(self, directory: Directory | None = None) -> CapcomAppServer:
        """A second APP server whose NA service delivers the custom NA blob in RAM."""

        service = AppService(
            config=AppServiceConfig(
                data_directory=self.data_directory,
                profile=MONSTER_HUNTER_NA,
                game_host='192.0.2.10',
                ram_patch_na=(DATA_DIRECTORY / 'patches' / 'na_custom_1002.bin').read_bytes(),
            ),
            flows=AppFlowTracker(),
            directory=directory or self.directory,
            online_players=self.online,
            records=self.records,
        )
        server = CapcomAppServer(
            host='127.0.0.1',
            port=0,
            titles={APP_BUILD_NA: service, APP_BUILD_EU: service},
        )
        server.start()
        self.addCleanup(server.stop)
        return server

    def _na_ram_open(
        self, server: CapcomAppServer, version: bytes = NA_STOCK_STAMP, build: int = APP_BUILD_NA
    ) -> tuple[socket.socket, bytearray]:
        """Answer 1002 and open a WORLD connection; an NA-flow client may get 6001 first."""

        client, buffer = self._ram_client(server)
        opening = self._receive(client, buffer)
        self.assertEqual((frame_command(opening), frame_payload(opening)), (0x1002, b'\x00\x00'))
        reply = (
            encode_app_field(b'hunter', 2)
            + bytes([build, 0x11])
            + encode_app_field(version, 2)
            + bytes(4)
        )
        client.sendall(app_frame(2, 0x1002, sequence=2, payload=reply))
        client.sendall(app_frame(1, 0x1007, sequence=3))
        opened = self._receive(client, buffer)
        # An EU client is pushed 6001 first; NA clients open with 1007.
        while frame_command(opened) == 0x6001:
            opened = self._receive(client, buffer)
        self.assertEqual(
            (frame_command(opened), frame_sequence(opened), frame_payload(opened)),
            (0x1007, 3, b'\x00'),
        )
        return client, buffer

    def _na_ram_world_list(self, client: socket.socket, buffer: bytearray) -> bytes:
        client.sendall(app_frame(1, 0x6503, sequence=4, payload=struct.pack('>2H', 0, 16)))
        page = self._receive(client, buffer)
        self.assertEqual((frame_command(page), frame_sequence(page)), (0x6503, 4))
        return page

    def _na_ram_final_page(self, client: socket.socket, buffer: bytearray) -> bytes:
        client.sendall(app_frame(1, 0x6503, sequence=5, payload=struct.pack('>2H', 8, 8)))
        page = self._receive(client, buffer)
        self.assertEqual((frame_command(page), frame_sequence(page)), (0x6503, 5))
        return page

    def test_na_ram_patch_burst_delivers_the_custom_blob(self) -> None:
        server = self._na_ram_server()
        client, buffer = self._na_ram_open(server)
        page = self._na_ram_world_list(client, buffer)
        blob = (DATA_DIRECTORY / 'patches' / 'na_custom_1002.bin').read_bytes()
        worlds = [(world.host or '192.0.2.10', world.name, world.description) for world in self.directory.worlds]
        prelude_plan = build_na_ram_patch(blob, worlds, sequence=4)
        self.assertEqual(frame_payload(page), prelude_plan.prelude)
        self.assertEqual(struct.unpack_from('>2HB', prelude_plan.prelude), (16, 0, 8))

        final_page = self._na_ram_final_page(client, buffer)
        plan = build_na_ram_patch(blob, worlds, sequence=5)
        self.assertEqual(frame_payload(final_page), plan.reply)
        self.assertEqual(struct.unpack_from('>2HB', plan.reply), (2, 8, 9))
        for expected in plan.frames:
            self.assertEqual(self._receive(client, buffer), expected)

        # Every injected page leaves a request behind: the first final page
        # asks for the world status, the guard-clearing 6204 asks for the
        # Market, the uploads ask for the next chunk, the prelude asks for the
        # next World page, the handler and guard pages ask for the Land list,
        # and the handler and guard writes ask for another chunk.  The first
        # 6502 is answered - its reply follows the burst, so the shellcode runs
        # - and the trigger page's duplicate 6502 stays unanswered.
        sequence = 6
        client.sendall(app_frame(1, 0x6502, sequence=sequence, payload=b'\x00'))
        reply = self._receive(client, buffer)
        self.assertEqual((frame_command(reply), frame_sequence(reply)), (0x6502, sequence))
        self.assertEqual(frame_payload(reply), b'\x01\x00\x00\x00\x01\x00\x00\x00\x00')
        sequence += 1
        client.sendall(app_frame(1, 0x6211, sequence=sequence))
        sequence += 1
        for _ in range(plan.ignore_6002 - 2):
            client.sendall(app_frame(1, 0x6002, sequence=sequence))
            sequence += 1
        client.sendall(app_frame(1, 0x6503, sequence=sequence, payload=struct.pack('>2H', 8, 8)))
        sequence += 1
        client.sendall(app_frame(1, 0x6403, sequence=sequence))
        sequence += 1
        client.sendall(app_frame(1, 0x6002, sequence=sequence))
        sequence += 1
        client.sendall(app_frame(1, 0x6403, sequence=sequence))
        sequence += 1
        client.sendall(app_frame(1, 0x6002, sequence=sequence))
        sequence += 1
        client.sendall(app_frame(1, 0x6502, sequence=sequence, payload=b'\x00'))

        # The trigger page's duplicate 6502 stays unanswered; the real 6502
        # handler then asks for the next World's status, which the service
        # answers.
        for offset in (1, 2):
            client.sendall(app_frame(1, 0x6502, sequence=sequence + offset, payload=b'\x00'))
            extra = self._receive(client, buffer)
            self.assertEqual(
                (frame_command(extra), frame_sequence(extra)), (0x6502, sequence + offset)
            )

        # The burst runs once per connection; later World pages are normal.
        client.sendall(app_frame(1, 0x6503, sequence=sequence + 3, payload=struct.pack('>2H', 0, 16)))
        later = self._receive(client, buffer)
        self.assertEqual((frame_command(later), frame_sequence(later)), (0x6503, sequence + 3))
        self.assertEqual(struct.unpack_from('>2HB', frame_payload(later)), (2, 0, 2))

    def test_na_ram_patch_skips_the_beta_a_patched_client_tles_and_eu(self) -> None:
        # Only the stock NA stamp may burst.  The beta and an already patched
        # client answer normally; TLES and EU never take the NA contract.
        for build, version in (
            (APP_BUILD_NA, NA_PUBLIC_BETA_VERSION),
            (APP_BUILD_NA, BLOB_MAGIC),
            (APP_BUILD_EU, TLES_STAMP),
            (APP_BUILD_EU, EU_STOCK_STAMP),
        ):
            with self.subTest(version=version):
                server = self._na_ram_server()
                client, buffer = self._na_ram_open(server, version=version, build=build)
                page = self._na_ram_world_list(client, buffer)
                self.assertEqual(struct.unpack_from('>2HB', frame_payload(page)), (2, 0, 2))
                client.sendall(app_frame(1, 0x6503, sequence=6, payload=struct.pack('>2H', 0, 16)))
                later = self._receive(client, buffer)
                self.assertEqual((frame_command(later), frame_sequence(later)), (0x6503, 6))

    @staticmethod
    def _long_worlds() -> dict:
        return {
            f'Long World {index}': {
                'host': '' if index == 0 else f'world{index}.example',
                'description': chr(ord('a') + index) * 255,
                'lands': [{'key': f'AA{index}', 'name': 'First', 'areas': 1}],
            }
            for index in range(3)
        }

    def test_ram_patch_falls_back_when_the_page_is_too_large(self) -> None:
        # Three 255-byte World descriptions cannot fit the client's 768-byte
        # body, so the service keeps the normal paged reply instead of sending
        # a priming page the client would truncate.
        with patch.dict('os.environ', {'OPENSNAP_MH_WORLDS': json.dumps(self._long_worlds())}):
            directory = read_directory(max_players_per_town=8)
        server = self._ram_server(directory)
        client, buffer = self._ram_client(server)
        self._ram_open(client, buffer, EU_STOCK_STAMP)
        page = self._ram_world_list(client, buffer, sequence=3)
        self.assertEqual(struct.unpack_from('>2HB', frame_payload(page)), (3, 0, 3))

    def test_na_ram_patch_falls_back_when_the_page_is_too_large(self) -> None:
        # Three 255-byte World descriptions cannot fit the client's 768-byte
        # body, so the service keeps the normal paged reply instead of sending
        # a priming page the client would truncate.
        with patch.dict('os.environ', {'OPENSNAP_MH_WORLDS': json.dumps(self._long_worlds())}):
            directory = read_directory(max_players_per_town=8)
        server = self._na_ram_server(directory)
        client, buffer = self._na_ram_open(server)
        page = self._na_ram_world_list(client, buffer)
        self.assertEqual(struct.unpack_from('>2HB', frame_payload(page)), (3, 0, 3))


class EuDescriptionsPatchTests(unittest.TestCase):
    """The shipped EU patch, read the way the applier reads it (EU `0x00248350`)."""

    def test_shipped_blob_matches_its_builder(self) -> None:
        from tools.monsterhunter.build_eu_patch import build

        self.assertEqual(build(), EU_PATCH.blob)

    def _entries(self) -> list[tuple[int, int, bytes]]:
        blob = EU_PATCH.blob
        self.assertEqual(blob[:8], b'M-HUNTER')
        count, = struct.unpack_from('<I', blob, 8)
        offset, entries = 12, []
        for _ in range(count):
            key, dest, length = struct.unpack_from('<3I', blob, offset)
            offset += 12
            self.assertFalse(length & 0x80000000)
            entries.append((key, dest, blob[offset:offset + length]))
            offset = (offset + length + 3) & ~3
        self.assertEqual(offset, len(blob))
        return entries

    def _text(self, address: int) -> str:
        blob = EU_PATCH.blob
        start = address - EU_RESIDENT_BLOB
        return blob[start:blob.index(b'\x00', start)].decode('utf-8')

    def test_key_0_applies_the_text_overlay_slot(self) -> None:
        pokes = [(dest, data) for key, dest, data in self._entries() if key == 0][1:]
        li_a0_3 = struct.pack('<I', 0x24040003)
        self.assertEqual(pokes, [(dest, li_a0_3) for dest in (0x002488A4, 0x002488BC, 0x00246C34, 0x00246C4C)])

    def test_capcom_fixes_jump_to_resident_copies_of_its_functions(self) -> None:
        official = (DATA_DIRECTORY / 'patches' / 'eu_official_1001.bin').read_bytes()
        hooks = [(key, dest, data) for key, dest, data in self._entries()[1:3]]
        self.assertEqual([(key, dest) for key, dest, _data in hooks], [(0x23, 0x005784D0), (0x00, 0x002A4050)])
        for (_key, _dest, data), source in zip(hooks, (0x40, 0xE0)):
            word, nop = struct.unpack('<2I', data)
            self.assertEqual((word >> 26, nop), (2, 0))  # j target; nop
            start = ((word & 0x03FFFFFF) << 2 | EU_RESIDENT_BLOB & 0xF0000000) - EU_RESIDENT_BLOB
            self.assertEqual(EU_PATCH.blob[start:start + 0xA0], official[source:source + 0xA0])

    def test_each_language_points_both_descriptions_at_resident_text(self) -> None:
        entries = self._entries()
        self.assertEqual(entries[0][0], 0xFFFFFFFF)
        for key in range(0x31, 0x36):
            voucher, sword = [data for entry_key, _dest, data in entries if entry_key == key]
            self.assertEqual(self._text(struct.unpack('<I', voucher)[0]).count('\n'), 2)
            lines = [self._text(address) for address in struct.unpack('<3I', sword)]
            self.assertTrue(lines[2].rstrip().endswith('GMR') or lines[2].rstrip().endswith('GMR.'))
        english = [data for key, _dest, data in entries if key == 0x31]
        self.assertEqual(
            self._text(struct.unpack('<I', english[0])[0]),
            "For those about to rock. \nGMR salutes you. \nWe're rocking right along. ",
        )

    def test_texts_fit_the_description_boxes(self) -> None:
        texts = [
            line
            for key, _dest, data in self._entries() if key in range(0x31, 0x36)
            for address in struct.unpack(f'<{len(data) // 4}I', data)
            for line in self._text(address).split('\n')
        ]
        self.assertEqual(len(texts), 5 * (3 + 3))
        for line in texts:
            # Fixed-width Latin-1 font; 28 visible characters per line (item and weapon boxes).
            self.assertLessEqual(len(line.rstrip(' ')), 28, line)
            line.encode('latin-1')
            self.assertNotIn('%', line)


class NaPatchTests(unittest.TestCase):
    """The shipped NA patch (`tools/monsterhunter/build_na_patch.py`)."""

    @unittest.skipUnless(shutil.which('mipsel-linux-gnu-as'), 'needs the MIPS binutils')
    def test_shipped_blob_matches_its_builder(self) -> None:
        from tools.monsterhunter.build_na_patch import build

        self.assertEqual(build(), NA_PATCH.blob)

    @unittest.skipUnless((DATA_DIRECTORY.parents[1] / 'slus_208.96').is_file(), 'needs slus_208.96')
    def test_main_elf_pokes_replace_the_expected_words(self) -> None:
        from tools.monsterhunter.build_na_patch import original_words

        # The NA ELF's one loaded segment: file offset 0x180 -> RAM 0x100000.
        image = (DATA_DIRECTORY.parents[1] / 'slus_208.96').read_bytes()
        for key, dest, original in original_words():
            if key == 0:
                offset = dest - 0x100000 + 0x180
                self.assertEqual(image[offset:offset + len(original)], original, hex(dest))

    def test_blob_keeps_its_code_resident_and_widens_the_reply_loop_last(self) -> None:
        blob = NA_PATCH.blob
        self.assertEqual(blob[:8], b'M-HUNTER')
        count, = struct.unpack_from('<I', blob, 8)
        offset, entries = 12, []
        for _ in range(count):
            key, dest, length = struct.unpack_from('<3I', blob, offset)
            offset += 12
            entries.append((key, dest, blob[offset:offset + length]))
            offset = (offset + length + 3) & ~3
        self.assertEqual(offset, len(blob))
        self.assertLessEqual(len(blob), 0x10000)
        self.assertEqual(entries[0][0], 0xFFFFFFFF)
        self.assertEqual({key for key, _dest, _data in entries[1:]}, {0x00, 0x11, 0x23})
        self.assertEqual(entries[-1][1:], (0x002B4C4C, struct.pack('<I', 0x29420044)))


if __name__ == '__main__':
    unittest.main()
