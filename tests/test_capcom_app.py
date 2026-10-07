"""Capcom APP process: routing by client build, and the game server -> APP channel (online players)."""

from pathlib import Path
import socket
import tempfile
import threading
import unittest

from opensnap.core.sessions import Session
from opensnap.plugins.registry import PLUGIN_FACTORIES
from opensnap.protocol.models import Endpoint
from opensnap.storage.sql import SqlOnlinePlayerStore
from opensnap.storage.sqlite import SqliteConnection
from opensnap_app import APPS, run_app
from opensnap_app.capcom.connection import AppConnection
from opensnap_app.capcom.monsterhunter.flows import AppFlowTracker, AppPhase
from opensnap_app.capcom.protocol import (
    APP_DIRECTION_REPLY,
    AppCodecError,
    ConnectAnswer,
    app_frame,
    encode_app_field,
    frame_command,
    frame_payload,
    pop_app_frame,
    read_connect_answer,
)
from opensnap_app.capcom.server import CapcomAppServer


def _answer(build: int) -> bytes:
    """A client's 1002 answer: `field login, u8 build, u8 flags, field version, 4 x u8`."""

    payload = encode_app_field(b'PLAYER', 3) + bytes([build, 1]) + encode_app_field(b'0404141723', 3) + bytes(4)
    return app_frame(APP_DIRECTION_REPLY, 0x1002, sequence=3, payload=payload)


def _session(session_id: int, host: str = '10.0.0.1') -> Session:
    return Session(session_id=session_id, user_id=session_id, username=f'p{session_id}', endpoint=Endpoint(host, 4000))


class _EchoTitle:
    """Title service that answers one request with its payload, then returns."""

    def __init__(self) -> None:
        self.answers: list[ConnectAnswer] = []
        self.done = threading.Event()

    def serve(self, connection: AppConnection, answer: ConnectAnswer) -> None:
        self.answers.append(answer)
        request = connection.receive()
        connection.reply(request, frame_payload(request))
        self.done.set()


class CapcomAppServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.titles = {1: _EchoTitle(), 2: _EchoTitle()}
        server = CapcomAppServer(host='127.0.0.1', port=0, titles=self.titles)
        server.start()
        self.addCleanup(server.stop)
        self.client = socket.create_connection(server.address, timeout=5)
        self.addCleanup(self.client.close)
        self.buffer = bytearray()

    def _receive(self) -> bytes | None:
        while (frame := pop_app_frame(self.buffer)) is None:
            data = self.client.recv(65535)
            if not data:
                return None
            self.buffer.extend(data)
        return frame

    def _open(self, build: int) -> None:
        opening = self._receive()
        self.assertEqual((frame_command(opening), frame_payload(opening)), (0x1002, b'\x00\x00'))
        # The client's first request may arrive in the same segment as its answer.
        self.client.sendall(_answer(build) + app_frame(1, 0x6101, sequence=4, payload=b'ping'))

    def test_each_build_reaches_its_title(self) -> None:
        self._open(2)
        self.assertEqual(frame_payload(self._receive()), b'ping')
        self.assertTrue(self.titles[2].done.wait(5))
        self.assertEqual(self.titles[1].answers, [])
        self.assertEqual((self.titles[2].answers[0].login, self.titles[2].answers[0].build), (b'PLAYER', 2))
        self.assertIsNone(self._receive())

    def test_unknown_build_is_closed(self) -> None:
        self._open(9)
        self.assertIsNone(self._receive())


class CapcomAppWireTests(unittest.TestCase):
    def test_connect_answer_needs_the_1002_command_and_build(self) -> None:
        answer = read_connect_answer(_answer(3))
        self.assertEqual((answer.build, answer.flags, answer.version), (3, 1, b'0404141723'))
        with self.assertRaises(AppCodecError):
            read_connect_answer(app_frame(APP_DIRECTION_REPLY, 0x1004))
        with self.assertRaises(AppCodecError):
            read_connect_answer(app_frame(APP_DIRECTION_REPLY, 0x1002, sequence=3, payload=encode_app_field(b'P', 3)))

    def test_capcom_is_the_companion_app_of_the_capcom_games(self) -> None:
        self.assertIn('capcom', APPS)
        companions = {name: plugin.companion_app for name, plugin in PLUGIN_FACTORIES.items()}
        self.assertEqual(companions['monsterhunter'], 'capcom')
        self.assertEqual(companions['outbreak'], 'capcom')
        self.assertIsNone(companions['automodellista'])
        with self.assertRaises(SystemExit):
            run_app('unknown')


class OnlinePlayerChannelTests(unittest.TestCase):
    """The APP flow tracker reading the game server's logins."""

    def setUp(self) -> None:
        temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temp_directory.cleanup)
        connection = SqliteConnection(Path(temp_directory.name) / 'shared.sqlite')
        self.addCleanup(connection.close)
        self.online = SqlOnlinePlayerStore(connection)

    def test_tracker_follows_each_login_once(self) -> None:
        flows = AppFlowTracker()
        self.online.login('monsterhunter', _session(1))
        flows.sync(self.online.list('monsterhunter'))
        self.assertEqual(flows.claim('10.0.0.1'), (1, AppPhase.WORLD))
        flows.release('10.0.0.1', 1)

        # Seen logins are not armed again: a later reconnect stays unattributed.
        flows.sync(self.online.list('monsterhunter'))
        self.assertEqual(flows.claim('10.0.0.1'), (None, AppPhase.LAND))

        # A repeated KICS (Land entry) hands the next connection to the player again.
        self.online.login('monsterhunter', _session(1))
        flows.sync(self.online.list('monsterhunter'))
        self.assertEqual(flows.claim('10.0.0.1'), (1, AppPhase.LAND))

        self.online.logout(1)
        flows.sync(self.online.list('monsterhunter'))
        self.assertEqual(flows.claim('10.0.0.1'), (None, AppPhase.APP1))


if __name__ == '__main__':
    unittest.main()
