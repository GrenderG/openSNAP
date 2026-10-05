"""Monster Hunter plugin flow tests."""

from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from opensnap.config import StorageConfig, UserConfig, default_app_config
from opensnap.core.engine import SnapProtocolEngine
from opensnap.core.sessions import Session
from opensnap.plugins.monsterhunter import MonsterHunterPlugin
from opensnap.plugins.monsterhunter.app import AppFlowTracker, AppPhase, AppService, AppServiceConfig
from opensnap.plugins.monsterhunter.app.codec import (
    app_frame,
    decode_app_field,
    encode_app_field,
    frame_command,
    frame_payload,
    frame_sequence,
    pop_app_frame,
)
from opensnap.plugins.monsterhunter.app.content import EventCatalog, InformationPages, MarketState
from opensnap.plugins.monsterhunter.app.records import build_record_page
from opensnap.plugins.monsterhunter.directory import Directory, read_directory
from opensnap.plugins.monsterhunter.profile import MONSTER_HUNTER_NA
from opensnap.protocol import commands
from opensnap.protocol.codec import decode_datagram, encode_messages
from opensnap.protocol.constants import (
    FLAG_CHANNEL_BITS,
    FLAG_MULTI,
    FLAG_RELIABLE,
    FLAG_ROOM,
)
from opensnap.protocol.models import Endpoint, SnapMessage
from opensnap.storage.sql import SqlRecordStore
from opensnap.storage.sqlite import SqliteConnection
from tests.support import MONSTER_HUNTER_NA_TITLE_CODE, login_client_payload, serving

TOWN_RULES = (3 << 8) | 4
QUEST_RULES = 2
QUEST_STARTED_RULES = 0x40000002
AREA_ID = 1


def _profile(marker: int, *, departing: bool = False) -> bytes:
    profile = bytearray([marker]) * 140
    profile[136:138] = b'\x03\x05' if departing else b'\x00\x00'
    return bytes(profile)


class MonsterHunterFlowTests(unittest.TestCase):
    """Town, quest departure and return flow through the protocol engine."""

    def setUp(self) -> None:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        users = tuple(UserConfig(user_id=index + 1, username=f'hunter{index}', password='1111') for index in range(3))
        self.config = replace(
            serving(default_app_config(), 'monsterhunter'),
            storage=StorageConfig(backend='sqlite', sqlite_path=f'{temp_directory.name}/mh.sqlite'),
            users=users,
        )
        self.plugin = MonsterHunterPlugin()
        self.engine = SnapProtocolEngine(config=self.config, plugin=self.plugin, role='combined')
        self.addCleanup(self.engine.close)
        self.sequences: dict[int, int] = {}
        self.players = [self._login(f'hunter{index}', f'10.0.0.{index + 1}') for index in range(3)]

    def _login(self, username: str, host: str) -> tuple[Endpoint, int]:
        endpoint = Endpoint(host=host, port=5000)
        login = self._send_raw(
            endpoint,
            0,
            commands.CMD_LOGIN_CLIENT,
            login_client_payload(f'{username}\n'.encode(), MONSTER_HUNTER_NA_TITLE_CODE),
            FLAG_CHANNEL_BITS,
        )
        session_id = login[0].session_id
        kics = self._send((endpoint, session_id), commands.CMD_LOGIN_TO_KICS, bytes(0x130), FLAG_CHANNEL_BITS)
        self.assertEqual(kics[0].command, commands.CMD_RESULT_LOGIN_TO_KICS)
        self._send((endpoint, session_id), commands.CMD_JOIN, struct.pack('>L', AREA_ID), FLAG_CHANNEL_BITS)
        return endpoint, session_id

    def _send_raw(self, endpoint, session_id, command, payload, type_flags, sequence=None) -> list[SnapMessage]:
        if sequence is None:
            sequence = self.sequences.get(session_id, 0) + 1
            self.sequences[session_id] = sequence
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
        return result.messages

    def _send(self, player, command, payload=b'', type_flags=FLAG_ROOM | FLAG_RELIABLE, sequence=None):
        endpoint, session_id = player
        return self._send_raw(endpoint, session_id, command, payload, type_flags, sequence)

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
            payload=_profile(1),
            size_word_override=(flags | FLAG_MULTI) | (16 + len(_profile(1))),
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
        profile = replace(child, command=commands.CMD_CHANGE_USER_PROPERTY, sequence_number=27, payload=_profile(1))
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
        self._publish(player, _profile(1))
        before = session.last_incoming_sequence
        self._send(player, commands.CMD_SEND, bytes.fromhex('0000'), FLAG_ROOM, sequence=self.sequences[session_id] + 10)
        self.assertEqual(session.last_incoming_sequence, before)
        town_id = self._create(player, TOWN_RULES)
        self.assertEqual(self._roster(player, town_id), [session_id])
        self.assertEqual(session.last_incoming_sequence, self.sequences[session_id])

    def _publish(self, player, profile: bytes) -> list[SnapMessage]:
        return self._send(player, commands.CMD_CHANGE_USER_PROPERTY, profile)

    def _create(self, player, rules: int) -> int:
        payload = bytearray(0x2C)
        payload[0:4] = b'Town'
        payload[0x10:0x14] = struct.pack('>L', 4)
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
        return [struct.unpack_from('>L', reply.payload, 12 + index * 164 + 16)[0] for index in range(count)]

    def _town_with_residents(self) -> int:
        host, guest, _ = self.players
        for index, player in enumerate(self.players):
            self._publish(player, _profile(index + 1))
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
        self._publish(host, _profile(1, departing=True))
        self._publish(guest, _profile(2, departing=True))
        self._send(guest, commands.CMD_LEAVE)
        self._join(guest, quest_id)
        self._stat(host, QUEST_STARTED_RULES)
        return quest_id

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

    def test_departing_hunters_stay_listed_in_their_town(self) -> None:
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)

        self._publish(host, _profile(1, departing=True))
        leave = self._send(host, commands.CMD_LEAVE)

        self.assertEqual([message.command for message in leave], [commands.CMD_RESULT_WRAPPER])
        self.assertIn(host[1], self._roster(waiting, town_id))
        attribute = self._send(waiting, commands.CMD_QUERY_ATTRIBUTE, struct.pack('>L', town_id))[0]
        self.assertEqual(struct.unpack('>L4sL', attribute.payload)[2], 3)
        self.assertIn(guest[1], self._roster(waiting, town_id))

    def test_solo_host_departure_keeps_its_town_listing(self) -> None:
        # Live NA solo order: selector 3 to itself, room leave, quest room
        # CREATE, then STAT and only afterwards the 3/5 profile.
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        preparation = struct.pack('>2L', 1, host[1]) + bytes((0, 0, 3, 0, 0, 0, 0, 0))
        self._send(host, commands.CMD_SEND_TARGET, preparation)

        leave = self._send(host, commands.CMD_LEAVE)

        self.assertEqual([message.command for message in leave], [commands.CMD_RESULT_WRAPPER])
        self.assertIn(host[1], self._roster(waiting, town_id))
        quest_id = self._create(host, QUEST_RULES)
        self._stat(host, QUEST_STARTED_RULES)
        self._publish(host, _profile(1, departing=True))
        self.assertIn(host[1], self._roster(guest, town_id))

        # Back from the quest, the host returns to the Town it never left.
        self._stat(host, TOWN_RULES)
        self.assertEqual(set(self._roster(host, quest_id)), {host[1], guest[1], waiting[1]})

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

    def test_plain_leave_announces_departure_and_unlists(self) -> None:
        town_id = self._town_with_residents()
        host, guest, _ = self.players

        messages = self._send(guest, commands.CMD_LEAVE)

        notices = [message for message in messages if message.command == commands.CMD_LEAVE]
        self.assertEqual([notice.endpoint for notice in notices], [host[0]])
        self.assertEqual(notices[0].payload, struct.pack('>L', guest[1]))
        self.assertNotIn(guest[1], self._roster(host, town_id))

    def test_quest_return_moves_each_hunter_back_to_its_town(self) -> None:
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        quest_id = self._depart(host, guest, town_id)
        self.assertEqual(set(self._roster(waiting, town_id)), {host[1], guest[1], waiting[1]})

        guest_return = self._stat(guest, TOWN_RULES)

        self.assertEqual([message.command for message in guest_return], [commands.CMD_RESULT_WRAPPER])
        # The returning client keeps using its quest room id as its Town id.
        self.assertEqual(set(self._roster(guest, quest_id)), {host[1], guest[1], waiting[1]})
        relays = [message for message in self._send(guest, commands.CMD_SEND, b'\x0b\xf0' + bytes(11))
                  if message.command == commands.CMD_SEND]
        self.assertEqual([relay.endpoint for relay in relays], [waiting[0]])

        self._stat(host, TOWN_RULES)
        rooms = self._send(waiting, commands.CMD_QUERY_GAME_ROOMS, struct.pack('>L', AREA_ID), FLAG_CHANNEL_BITS)
        room_ids = [struct.unpack_from('>L', rooms[0].payload, 12 + index * 36 + 32)[0]
                    for index in range(struct.unpack_from('>L', rooms[0].payload, 8)[0])]
        self.assertEqual(room_ids, [town_id])

    def test_started_quest_members_get_no_lobby_callbacks(self) -> None:
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        self._depart(host, guest, town_id)

        messages = self._publish(guest, _profile(9))

        recipients = {message.endpoint for message in messages if message.command == commands.CMD_CHANGE_USER_PROPERTY}
        self.assertEqual(recipients, {waiting[0]})

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

        # The client now runs game.bin: no echo reply and no property result.
        self.assertEqual(self._send(host, commands.CMD_SEND_ECHO, echo, type_flags=FLAG_ROOM), [])
        replies = self._publish(host, _profile(5))
        self.assertNotIn(host[0], [message.endpoint for message in replies])
        self.assertEqual(self.plugin._players[host[1]].profile, _profile(5))  # noqa: SLF001

        # After the return STAT the lobby is back and keepalives are answered.
        self._stat(host, TOWN_RULES)
        self.assertEqual(
            [message.command for message in self._send(host, commands.CMD_SEND_ECHO, echo, type_flags=FLAG_ROOM)],
            [commands.CMD_SEND_ECHO],
        )

    def test_abandoning_a_started_quest_unlists_the_hunter(self) -> None:
        town_id = self._town_with_residents()
        host, guest, waiting = self.players
        self._join(waiting, town_id)
        self._depart(host, guest, town_id)

        messages = self._send(guest, commands.CMD_LEAVE)

        notices = [message for message in messages if message.command == commands.CMD_LEAVE]
        self.assertEqual([notice.endpoint for notice in notices], [waiting[0]])
        self.assertNotIn(guest[1], self._roster(waiting, town_id))

    def test_join_callback_carries_member_profile(self) -> None:
        host, guest, _ = self.players
        self._publish(host, _profile(1))
        self._publish(guest, _profile(2))
        town_id = self._create(host, TOWN_RULES)

        messages = self._join(guest, town_id)

        callback = [message for message in messages if message.command == commands.CMD_JOIN][0]
        self.assertEqual(callback.endpoint, host[0])
        self.assertEqual(callback.payload[:16], b'hunter1'.ljust(16, b'\x00'))
        self.assertEqual(struct.unpack_from('>2L', callback.payload, 16), (guest[1], 140))
        self.assertEqual(callback.payload[24:], _profile(2))

    def test_exact_reliable_create_retry_replays_the_same_room(self) -> None:
        host = self.players[0]
        payload = bytearray(0x2C)
        payload[0x28:0x2C] = struct.pack('>L', TOWN_RULES)

        first = self._send(host, commands.CMD_CREATE_GAME_ROOM, bytes(payload), sequence=50)
        retry = self._send(host, commands.CMD_CREATE_GAME_ROOM, bytes(payload), sequence=50)

        first_result, = [message for message in first if message.command == commands.CMD_RESULT_WRAPPER]
        retry_result, = [message for message in retry if message.command == commands.CMD_RESULT_WRAPPER]
        self.assertEqual(first_result.payload, retry_result.payload)
        self.assertEqual(first_result.sequence_number, retry_result.sequence_number)
        # The retry changes no Town, so it announces nothing again.
        self.assertFalse([message for message in retry if message.command == commands.CMD_CREATE_GAME_ROOM])
        rooms = self._send(host, commands.CMD_QUERY_GAME_ROOMS, struct.pack('>L', AREA_ID), FLAG_CHANNEL_BITS)
        self.assertEqual(struct.unpack_from('>L', rooms[0].payload, 8)[0], 1)

    def test_exact_join_retry_replays_only_the_joiners_result(self) -> None:
        host, guest, _ = self.players
        self._publish(host, _profile(1))
        self._publish(guest, _profile(2))
        town_id = self._create(host, TOWN_RULES)

        first = self._send(guest, commands.CMD_JOIN, struct.pack('>L', town_id), sequence=60)
        retry = self._send(guest, commands.CMD_JOIN, struct.pack('>L', town_id), sequence=60)

        self.assertTrue([m for m in first if m.command == commands.CMD_JOIN and m.endpoint == host[0]])
        # MH re-applies a repeat of its newest unreliable sequence, so the host's
        # join callback is not sent twice; the guest gets its original result.
        self.assertEqual([(m.endpoint, m.command) for m in retry], [(guest[0], commands.CMD_RESULT_WRAPPER)])
        original, = [m for m in first if m.command == commands.CMD_RESULT_WRAPPER]
        self.assertEqual((retry[0].payload, retry[0].sequence_number), (original.payload, original.sequence_number))

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
            self._publish(player, _profile(index + 1))
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
        self.assertEqual(struct.unpack_from('>3L', roster.payload), (AREA_ID, 2, 2))

    def test_friend_search_reports_area_and_room(self) -> None:
        town_id = self._town_with_residents()
        searcher, friend, _ = self.players

        reply = self._send(searcher, commands.CMD_SEARCH_USERS_BY_NAME, pack16('hunter1-') + struct.pack('>2L', 8, 1),
                           FLAG_CHANNEL_BITS | FLAG_RELIABLE)[0]

        self.assertEqual(struct.unpack_from('>3L', reply.payload), (0, 1, 1))
        record = reply.payload[12:]
        self.assertEqual(record[:16], pack16('hunter1'))
        self.assertEqual(struct.unpack_from('>4L', record, 16), (1, AREA_ID, town_id, 140))
        self.assertEqual(record[32:], _profile(2))

        missing = self._send(searcher, commands.CMD_SEARCH_USERS_BY_NAME, pack16('nobody-') + struct.pack('>2L', 7, 1),
                             FLAG_CHANNEL_BITS | FLAG_RELIABLE)[0]
        self.assertEqual(missing.payload, struct.pack('>3L', 0, 0, 0))

    def test_unknown_area_join_is_rejected(self) -> None:
        reply = self._send(self.players[0], commands.CMD_JOIN, struct.pack('>L', 99), FLAG_CHANNEL_BITS)[0]
        self.assertEqual(struct.unpack('>2L', reply.payload)[1], 0x27)

    def test_area_join_is_refused_when_the_land_is_full(self) -> None:
        lands = [{'key': 'RED', 'areas': 2, 'capacity': 2}]
        with patch.dict('os.environ', {'OPENSNAP_MH_WORLDS': json.dumps({'W': {'lands': lands}})}):
            self.plugin._directory = read_directory(max_players_per_town=8)
        self._send(self.players[2], commands.CMD_LEAVE, b'', FLAG_CHANNEL_BITS)

        refused = self._send(self.players[2], commands.CMD_JOIN, struct.pack('>L', 2), FLAG_CHANNEL_BITS)[0]
        self.assertEqual(struct.unpack('>2L', refused.payload)[1], 0x27)
        # A hunter already counted in the Land may still move between its Areas.
        moved = self._send(self.players[0], commands.CMD_JOIN, struct.pack('>L', 2), FLAG_CHANNEL_BITS)[0]
        self.assertEqual(struct.unpack('>2L', moved.payload)[1], 0)

    def test_town_category_is_limited_to_21_towns(self) -> None:
        for index in range(21):
            self.engine._rooms.create_room(
                name=f'T{index}', password='', rules=TOWN_RULES, max_players=8, lobby_id=AREA_ID,
                host_session_id=0x1000 + index,
            )
        payload = bytearray(0x2C)
        payload[0x28:0x2C] = struct.pack('>L', TOWN_RULES)
        reply = self._send(self.players[1], commands.CMD_CREATE_GAME_ROOM, bytes(payload))[-1]
        self.assertEqual(struct.unpack('>2L', reply.payload)[1], 0x27)
        other_category = (1 << 24) | TOWN_RULES
        self.assertNotEqual(self._create(self.players[1], other_category), 0x27)

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
        from datetime import date, timedelta

        start = date(2026, 1, 1)
        days = [
            struct.unpack('>L', MarketState(lambda day=start + timedelta(days=offset): day).native_response())[0]
            for offset in range(12)
        ]
        self.assertEqual(days, [1, 0, 4, 0, 2, 0, 4, 0, 3, 0, 1, 0])

    def test_event_catalog_serves_754_byte_chunks(self) -> None:
        import hashlib

        events = self.root / 'events'
        events.mkdir()
        content = bytes(range(256)) * 4
        (events / 'event.bin').write_bytes(content)
        (events / 'manifest.json').write_text(json.dumps({
            'event_availability': 1,
            'catalog_key': 'KEY',
            'event_catalog': [{
                'file': 'event.bin',
                'size': len(content),
                'sha256': hashlib.sha256(content).hexdigest(),
            }],
        }))
        catalog = EventCatalog(self.root)
        self.assertEqual(catalog.availability(), struct.pack('>L', 1))
        self.assertEqual(catalog.catalog(lambda data: data), b'\x01KEY' + struct.pack('>HL', 1, len(content)))
        chunk = catalog.download(struct.pack('>HLH', 0, 754, 754), lambda data: data)
        self.assertEqual(chunk, struct.pack('>HL', 0, 754) + content[754:])
        with self.assertRaises(ValueError):
            catalog.download(struct.pack('>HLH', 0, 10, 754), lambda data: data)

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
        connection = SqliteConnection(self.data_directory / 'records.sqlite')
        self.addCleanup(connection.close)
        self.records = SqlRecordStore(connection)
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
                host='127.0.0.1',
                port=0,
                data_directory=self.data_directory,
                profile=MONSTER_HUNTER_NA,
                advertise_host='192.0.2.10',
                game_bind_host='0.0.0.0',
            ),
            flows=self.flows,
            directory=self.directory,
            land_population=lambda land: 4 * land.number,
            records=self.records,
            session=self.sessions.get,
        )
        self.service.start()
        self.addCleanup(self.service.stop)
        self.port = self.service._listener.getsockname()[1]

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

    def _open(self, client: socket.socket, buffer: bytearray, *, build: int = 2, flags: int = 1) -> None:
        """Answer the server's opening 1002 like the client build does (`0x002aeb40`)."""

        opening = self._receive(client, buffer)
        self.assertEqual((frame_command(opening), frame_payload(opening)), (0x1002, b'\x00\x00'))
        reply = encode_app_field(b'HUNTER', 2) + bytes([build, flags]) + encode_app_field(b'0404141723', 2) + bytes(4)
        client.sendall(app_frame(2, 0x1002, sequence=2, payload=reply))
        if build == 3:
            self._receive_version(client, buffer)

    def _receive_version(self, client: socket.socket, buffer: bytearray) -> None:
        """6001 is a server push (direction 0x10) with flag 0: no client patch."""

        self.assertEqual(self._receive(client, buffer), app_frame(0x10, 0x6001, payload=b'\x00'))

    def _exchange(self, client: socket.socket, buffer: bytearray, command: int, payload: bytes = b'') -> bytes:
        client.sendall(app_frame(1, command, sequence=5, payload=payload))
        reply = self._receive(client, buffer)
        self.assertEqual((frame_command(reply), frame_sequence(reply)), (command, 5))
        return frame_payload(reply)

    def test_world_then_land_progression(self) -> None:
        self.flows.arm_world(1, '127.0.0.1')
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
        self.flows.arm_world(1, '127.0.0.1')
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
        self.flows.arm_world(1, '127.0.0.1')
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
        self.assertEqual(frame_payload(self._receive(client, buffer)), struct.pack('>B2H', 2, 4, 8) + brave)

        # Players of a World hosted elsewhere are not visible here.
        sincere = encode_app_field(b'sincere.example', 12)
        client.sendall(app_frame(1, 0x6510, sequence=12, payload=b'\x01' + encode_app_field(b'CCC', 12) + sincere))
        self.assertEqual(frame_payload(self._receive(client, buffer)), struct.pack('>BH', 1, 0) + sincere)

    def test_world_connection_reaches_connection_timing(self) -> None:
        self.flows.arm_world(1, '127.0.0.1')
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
        self.assertEqual(self._exchange(client, buffer, 0x6320, b'\x00\x00\x00\x01'), b'\x00')
        self.assertEqual(self._exchange(client, buffer, 0x1004), b'')
        # APP1 does not advance any online flow.
        self.assertEqual(self.flows.claim('127.0.0.1'), (None, AppPhase.APP1))

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
        self.flows.arm_world(1, '127.0.0.1')
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

    def test_eu_quest_records_are_acknowledged_and_stored(self) -> None:
        self.flows.arm_world(1, '127.0.0.1')
        self.flows.advance('127.0.0.1', 1, AppPhase.LAND)
        client, buffer = self._connect()
        self._open(client, buffer, build=3, flags=0x15)

        counts = [0] * 34
        counts[3], counts[10] = 2, 1
        hunts = struct.pack('>BBH', 1, 12, 0x0105) + b''.join(
            struct.pack('>BL', index, count) for index, count in enumerate(counts)
        )
        self.assertEqual(self._exchange(client, buffer, 0x6307, hunts), b'')
        clear = struct.pack('>BBHBL', 2, 4, 0x0105, 0, 754)
        self.assertEqual(self._exchange(client, buffer, 0x6307, clear), b'')
        self.assertEqual(self._exchange(client, buffer, 0x6320, b'\x00\x00\x00\x01'), b'\x00')

        boards = self.records.best_by_board('monsterhunter', 10)
        self.assertEqual(
            [(record.player, record.score, dict(record.details)) for record in boards['hunts-261']],
            [('hunter', 3, {'hunter_rank': 12, 'monsters': {'3': 2, '10': 1}})],
        )
        self.assertEqual(
            [(record.player, record.score, dict(record.details)) for record in boards['clear-261']],
            [('hunter', 754, {'weapon_class': 4})],
        )

    def test_record_page_is_generated_from_the_records(self) -> None:
        self.records.add(game='monsterhunter', board='hunts-261', user_id=7, player='hunter', score=3, details={})
        self.records.add(game='monsterhunter', board='hunts-300', user_id=7, player='hunter', score=2, details={})
        self.records.add(game='monsterhunter', board='hunts-261', user_id=8, player='o<b>', score=9, details={})
        self.records.add(game='monsterhunter', board='clear-261', user_id=7, player='hunter', score=754, details={})
        self.flows.arm_world(1, '127.0.0.1')
        self.flows.advance('127.0.0.1', 1, AppPhase.LAND)
        client, buffer = self._connect()
        # EU lobby Information/Record: APP mode 3 asks for the path after `lbs://lbs/`.
        self._open(client, buffer, build=3, flags=0x14)
        name = b'03/04/DATABASE.HTM'
        info = self._exchange(client, buffer, 0x6101, bytes(4) + encode_app_field(name, 5))
        size = struct.unpack('>L', info[-4:])[0]
        field = encode_app_field(name, 5)
        page = b''
        for offset in range(0, size, 722):
            chunk = self._exchange(client, buffer, 0x6102, field + struct.pack('>LH', offset, 722))
            page += decode_app_field(chunk[len(field) + 4:], 5)[0]
        text = page.decode()
        self.assertEqual(len(page), size)
        self.assertTrue(text.startswith('<HTML>') and text.endswith('</HTML>\n'))
        # Monsters hunted, summed over quests, most first; names are escaped.
        self.assertIn('<TR><TD>1</TD><TD>o&lt;b&gt;</TD><TD>9</TD></TR>', text)
        self.assertIn('<TR><TD>2</TD><TD>hunter</TD><TD>5</TD></TR>', text)
        self.assertIn('<P>Quest 261</P>', text)
        self.assertIn('<TR><TD>1</TD><TD>hunter</TD><TD>12\'34"</TD></TR>', text)

    def test_na_record_menu_gets_the_same_page(self) -> None:
        self.records.add(game='monsterhunter', board='clear-261', user_id=7, player='hunter', score=754, details={})
        client, buffer = self._connect()
        # NA Record menu: APP mode 3 opens with 6101 right after 1002 (no 6001).
        self._open(client, buffer)
        name = b'02/DATABASE.HTM'
        info = self._exchange(client, buffer, 0x6101, bytes(4) + encode_app_field(name, 5))
        page = build_record_page(self.records, 'monsterhunter')
        self.assertEqual(struct.unpack('>L', info[-4:])[0], len(page))
        field = encode_app_field(name, 5)
        chunk = self._exchange(client, buffer, 0x6102, field + struct.pack('>LH', 0, 722))
        self.assertEqual(decode_app_field(chunk[len(field) + 4:], 5)[0], page[:722])

    def test_record_page_fits_the_lobby_buffer(self) -> None:
        for quest in range(400):
            for user in range(5):
                self.records.add(
                    game='monsterhunter', board=f'clear-{quest}', user_id=user, player=f'p{user}', score=60, details={}
                )
        page = build_record_page(self.records, 'monsterhunter')
        self.assertLessEqual(len(page), 0x8000 - 1)
        self.assertTrue(page.endswith(b'</BODY></HTML>\n'))

    def test_quest_record_without_a_single_player_is_acknowledged_only(self) -> None:
        self.flows.arm_world(1, '127.0.0.1')
        self.flows.arm_world(2, '127.0.0.1')
        for session_id in (1, 2):
            self.assertEqual(self.flows.claim('127.0.0.1')[0], session_id)
            self.flows.release('127.0.0.1', session_id)
        # Two players behind one address: later reconnects are unattributed.
        self.assertEqual(self.flows.claim('127.0.0.1'), (None, AppPhase.LAND))
        client, buffer = self._connect()
        self._open(client, buffer, build=3, flags=0x11)
        clear = struct.pack('>BBHBL', 2, 0, 1, 0, 60)
        self.assertEqual(self._exchange(client, buffer, 0x6307, clear), b'')
        self.assertEqual(self.records.best_by_board('monsterhunter', 10), {})

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
        self.flows.arm_world(1, '127.0.0.1')
        client, buffer = self._connect()
        self._open(client, buffer)
        # The next frame is the reply to the client's first request, not a 6001.
        self.assertEqual(self._exchange(client, buffer, 0x1007), b'\x00')


if __name__ == '__main__':
    unittest.main()
