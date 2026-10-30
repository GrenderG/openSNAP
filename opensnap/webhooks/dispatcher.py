"""Webhook transport: one queued worker thread posting game announcements."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request

_LOGGER = logging.getLogger('opensnap.webhooks')

FORMAT_DISCORD = 'discord'
FORMAT_JSON = 'json'
FORMATS = (FORMAT_DISCORD, FORMAT_JSON)
REQUEST_TIMEOUT_SECONDS = 10.0
# Discord rejects a message longer than this.
CONTENT_MAX_LENGTH = 2000
# Longest `Retry-After` honoured once on a 429 before the message is dropped.
RETRY_AFTER_MAX_SECONDS = 30.0
# Cloudflare in front of Discord rejects urllib's default `Python-urllib` agent.
USER_AGENT = 'openSNAP-webhooks (https://github.com/GrenderG/openSNAP)'
DAY_CHANGE_POLL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """One announcement: Discord markdown text plus its structured fields."""

    game: str
    kind: str
    content: str
    data: Mapping[str, object] = field(default_factory=dict)


class WebhookDispatcher:
    """Queue events and post them to one URL from a daemon thread.

    `send` never blocks or raises, so any thread may call it. Without a URL it
    drops every event and starts no thread. Events still queued at exit are lost.
    """

    def __init__(self, url: str = '', format_: str = FORMAT_DISCORD) -> None:
        if format_ not in FORMATS:
            raise ValueError(f'Unsupported webhook format {format_!r}; use one of: {", ".join(FORMATS)}.')
        self._url = url
        self._format = format_
        self._queue: queue.Queue[WebhookEvent] = queue.Queue()
        if url:
            threading.Thread(target=self._run, name='webhooks', daemon=True).start()

    @classmethod
    def from_env(cls) -> 'WebhookDispatcher':
        """Build from `OPENSNAP_WEBHOOK_URL` (empty disables) and `OPENSNAP_WEBHOOK_FORMAT`."""

        return cls(
            os.getenv('OPENSNAP_WEBHOOK_URL', '').strip(),
            os.getenv('OPENSNAP_WEBHOOK_FORMAT', '').strip().lower() or FORMAT_DISCORD,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    def send(self, event: WebhookEvent) -> None:
        if self._url:
            self._queue.put(event)

    def body(self, event: WebhookEvent) -> bytes:
        """Return the POST body of one event in the configured format."""

        content = event.content[:CONTENT_MAX_LENGTH]
        if self._format == FORMAT_JSON:
            payload: dict[str, object] = {
                'game': event.game, 'event': event.kind, 'content': content, 'data': dict(event.data),
            }
        else:
            # Player-chosen text (names, teams) must not ping anyone.
            payload = {'content': content, 'allowed_mentions': {'parse': []}}
        return json.dumps(payload).encode('utf-8')

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                self._post(event)
            except Exception:  # noqa: BLE001
                _LOGGER.exception('Webhook %s/%s failed.', event.game, event.kind)

    def _post(self, event: WebhookEvent) -> None:
        body = self.body(event)
        for attempt in range(2):
            request = urllib.request.Request(
                self._url,
                data=body,
                headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT},
                method='POST',
            )
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
                    return
            except urllib.error.HTTPError as exc:
                retry_after = _retry_after(exc)
                if exc.code != 429 or attempt or retry_after is None:
                    _LOGGER.warning('Webhook %s/%s rejected: HTTP %d.', event.game, event.kind, exc.code)
                    return
                time.sleep(retry_after)
            except (urllib.error.URLError, OSError) as exc:
                _LOGGER.warning('Webhook %s/%s not delivered: %s', event.game, event.kind, exc)
                return


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    """Seconds a 429 asks to wait, or None when absent or too long."""

    try:
        seconds = float(error.headers.get('Retry-After', ''))
    except ValueError:
        return None
    return seconds if 0 <= seconds <= RETRY_AFTER_MAX_SECONDS else None


class DayChangeTimer:
    """Call `callback` from a daemon thread whenever the local date changes.

    `today` must be the clock of whatever the callback announces, so the
    announcement matches what clients are served.
    """

    def __init__(
        self,
        callback: Callable[[], None],
        *,
        today: Callable[[], date] = date.today,
        poll_seconds: float = DAY_CHANGE_POLL_SECONDS,
    ) -> None:
        self._callback = callback
        self._today = today
        self._poll_seconds = poll_seconds
        self._day = today()
        self._stopped = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name='webhooks-day', daemon=True).start()

    def stop(self) -> None:
        self._stopped.set()

    def check(self) -> None:
        """Run the callback once if the date changed since the last check."""

        day = self._today()
        if day == self._day:
            return
        self._day = day
        try:
            self._callback()
        except Exception:  # noqa: BLE001
            _LOGGER.exception('Day change announcement failed.')

    def _run(self) -> None:
        while not self._stopped.wait(self._poll_seconds):
            self.check()
