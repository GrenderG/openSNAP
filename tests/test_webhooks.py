"""Webhook transport and the game announcements built on it."""

from dataclasses import replace
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

from opensnap.config import StorageConfig, default_app_config
from opensnap.plugins.automodellista.webhooks import AutoModellistaWebhooks
from opensnap.plugins.monsterhunter.profile import MONSTER_HUNTER_NA
from opensnap.plugins.monsterhunter.webhooks import MonsterHunterWebhooks
from opensnap.storage.factory import create_storage
from opensnap.webhooks import DayChangeTimer, WebhookDispatcher, WebhookEvent
from opensnap_app.capcom.monsterhunter.content import MARKET_DAY_NAMES, MarketState
from opensnap_app.capcom.monsterhunter.service import AppService, AppServiceConfig
from opensnap_web.games.automodellista.rankings import AutoModellistaRankings
from tests.support import RecordingDispatcher

EVENT = WebhookEvent(game='automodellista', kind='course_record', content='@everyone ' + 'x' * 3000, data={'lap_ms': 1})


class WebhookDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        requests: queue.Queue[tuple[dict[str, str], bytes]] = queue.Queue()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                requests.put((dict(self.headers), self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f'http://127.0.0.1:{server.server_address[1]}/hook'
        self.requests = requests

    def test_discord_body_mentions_nobody_and_fits_a_message(self) -> None:
        WebhookDispatcher(self.url).send(EVENT)
        headers, body = self.requests.get(timeout=5)
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertTrue(headers['User-Agent'].startswith('openSNAP'))
        self.assertEqual(json.loads(body), {'content': EVENT.content[:2000], 'allowed_mentions': {'parse': []}})

    def test_json_body_carries_the_event_fields(self) -> None:
        WebhookDispatcher(self.url, 'json').send(EVENT)
        _headers, body = self.requests.get(timeout=5)
        self.assertEqual(json.loads(body), {
            'game': 'automodellista', 'event': 'course_record', 'content': EVENT.content[:2000], 'data': {'lap_ms': 1},
        })

    def test_without_a_url_nothing_is_started(self) -> None:
        threads = threading.active_count()
        dispatcher = WebhookDispatcher()
        dispatcher.send(EVENT)
        self.assertFalse(dispatcher.enabled)
        self.assertEqual(threading.active_count(), threads)

    def test_unknown_format_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WebhookDispatcher(self.url, 'xml')


class DayChangeTimerTests(unittest.TestCase):
    def test_callback_runs_once_per_new_date(self) -> None:
        today = [date(2026, 10, 30)]
        calls: list[date] = []
        timer = DayChangeTimer(lambda: calls.append(today[0]), today=lambda: today[0])
        timer.check()
        today[0] = date(2026, 10, 31)
        timer.check()
        timer.check()
        self.assertEqual(calls, [date(2026, 10, 31)])


class GameAnnouncementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatcher = RecordingDispatcher()

    def test_monster_hunter_announces_market_day_and_missing_event(self) -> None:
        with tempfile.TemporaryDirectory() as data_directory:
            service = AppService(
                config=AppServiceConfig(data_directory=Path(data_directory), profile=MONSTER_HUNTER_NA, game_host=''),
                flows=MagicMock(),
                directory=MagicMock(),
                online_players=MagicMock(),
                records=MagicMock(),
                webhooks=MonsterHunterWebhooks(self.dispatcher, 'monsterhunter'),
            )
        service.announce_day()
        self.assertEqual(
            [(event.kind, dict(event.data)) for event in self.dispatcher.events],
            [('market_day', {'day': MARKET_DAY_NAMES[MarketState().current()]}), ('event_quest', {'title': None})],
        )

    def test_auto_modellista_announces_only_board_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                default_app_config(), storage=StorageConfig(backend='sqlite', sqlite_path=f'{directory}/am.sqlite')
            )
            storage = create_storage(config)
            self.addCleanup(storage.close)
            storage.accounts.create_user('racer', '1111')
            storage.accounts.create_user('rival', '1111')
            rankings = AutoModellistaRankings(
                game='automodellista',
                accounts=storage.accounts,
                records=storage.records,
                rows_by_board={'D': 10},
                webhooks=AutoModellistaWebhooks(self.dispatcher, 'automodellista'),
            )
            for login, lap_ms in (('racer', 83_456), ('rival', 90_000), ('rival', 81_002)):
                rankings.submit('D', f'{login:>15}{"TEAM":>15}{lap_ms:010d}')
        self.assertEqual([event.data['lap_ms'] for event in self.dispatcher.events], [83_456, 81_002])
        self.assertEqual(
            self.dispatcher.events[1].content,
            '>>> New course record on **course D**: **rival** [TEAM] with **1\'21"002**!',
        )


if __name__ == '__main__':
    unittest.main()
