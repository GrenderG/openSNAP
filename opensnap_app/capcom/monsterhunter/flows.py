"""APP connection phase tracking for the NA public beta, fed by the game server's logins."""

from collections import OrderedDict
from collections.abc import Iterable
from enum import StrEnum
import threading

from opensnap.core.online import OnlinePlayer

# A client: its address and the login of its 1002 answer, which is the
# username it logs in to SN@P with (beta `0x002acd30`, `0x0024798c`). The
# login tells apart consoles that share one address.
FlowKey = tuple[str, str]
# Clients whose last released phase is kept. Only the least recently seen is
# dropped, so a client loses its phase only after this many others connected.
MAX_FALLBACK_CLIENTS = 1024


class AppPhase(StrEnum):
    """Native APP connection kinds, in client order."""

    APP1 = 'APP1'
    WORLD = 'WORLD'
    POSTWORLD = 'POSTWORLD'
    LAND = 'LAND'


class AppFlowTracker:
    """Classify the NA public beta's APP connections, which carry no SNAP session id.

    Retail builds open each connection kind with its own request, so they need
    no tracker. The beta (`SLUS_291.10`) opens both its post-World and its Land
    connection with 6501 (modes 4-6, `0x002acd00`), so only the order tells
    them apart. After KICS login a session owns its own WORLD -> POSTWORLD ->
    LAND progression; connections from one client are attributed to that
    client's pending sessions in login order. Later LAND reconnects that cannot
    be attributed use the client's last released phase.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._login_serial_by_session: dict[int, int] = {}
        self._phase_by_session: dict[int, AppPhase] = {}
        self._key_by_session: dict[int, FlowKey] = {}
        self._pending_by_key: dict[FlowKey, list[int]] = {}
        self._active_by_key: dict[FlowKey, int] = {}
        # Flows continue without a session after MH's logout before World
        # selection, so these outlive sessions; bounded by recency instead.
        self._fallback_by_key: OrderedDict[FlowKey, AppPhase] = OrderedDict()

    def sync(self, players: Iterable[OnlinePlayer]) -> None:
        """Catch up with the game server: arm each login not seen yet, in login order, and forget logged-out players."""

        online = {player.session_id: player for player in players}
        with self._lock:
            for session_id in [session_id for session_id in self._login_serial_by_session if session_id not in online]:
                del self._login_serial_by_session[session_id]
                self.forget(session_id)
            for player in online.values():
                if self._login_serial_by_session.get(player.session_id) != player.login_serial:
                    self._login_serial_by_session[player.session_id] = player.login_serial
                    self.arm_world(player.session_id, (player.host, player.username))

    def arm_world(self, session_id: int, key: FlowKey) -> None:
        """Start the online APP flow for one logged-in session without rewinding it."""

        with self._lock:
            self._key_by_session[session_id] = key
            self._phase_by_session.setdefault(session_id, AppPhase.WORLD)
            pending = self._pending_by_key.setdefault(key, [])
            if self._active_by_key.get(key) != session_id and session_id not in pending:
                pending.append(session_id)

    def forget(self, session_id: int) -> None:
        """Drop one logged-out session; a client with no sessions restarts at APP1."""

        with self._lock:
            key = self._key_by_session.pop(session_id, None)
            self._phase_by_session.pop(session_id, None)
            if key is None:
                return
            if self._active_by_key.get(key) == session_id:
                self._active_by_key.pop(key, None)
            pending = self._pending_by_key.get(key, [])
            if session_id in pending:
                pending.remove(session_id)
            if key not in self._key_by_session.values():
                self._pending_by_key.pop(key, None)
                self._fallback_by_key.pop(key, None)

    def claim(self, key: FlowKey) -> tuple[int | None, AppPhase]:
        """Return the owner session and phase for one new APP connection."""

        with self._lock:
            active = self._active_by_key.get(key)
            if active is not None and active in self._phase_by_session:
                return active, self._phase_by_session[active]
            self._active_by_key.pop(key, None)

            pending = self._pending_by_key.get(key, [])
            while pending:
                session_id = pending.pop(0)
                if session_id in self._phase_by_session:
                    self._active_by_key[key] = session_id
                    return session_id, self._phase_by_session[session_id]
            phase = self._fallback_by_key.get(key)
            if phase is None:
                return None, AppPhase.APP1
            self._fallback_by_key.move_to_end(key)
            return None, phase

    def advance(self, key: FlowKey, session_id: int | None, phase: AppPhase) -> None:
        """Record the phase the next connection of this flow will use."""

        with self._lock:
            if session_id is None:
                self._remember_fallback(key, phase)
            elif session_id in self._phase_by_session:
                self._phase_by_session[session_id] = phase

    def release(self, key: FlowKey, session_id: int | None) -> None:
        """Finish one LAND connection; later unattributed reconnects use LAND."""

        with self._lock:
            if session_id is not None:
                if session_id in self._phase_by_session:
                    self._phase_by_session[session_id] = AppPhase.LAND
                if self._active_by_key.get(key) == session_id:
                    self._active_by_key.pop(key, None)
            self._remember_fallback(key, AppPhase.LAND)

    def _remember_fallback(self, key: FlowKey, phase: AppPhase) -> None:
        self._fallback_by_key[key] = phase
        self._fallback_by_key.move_to_end(key)
        while len(self._fallback_by_key) > MAX_FALLBACK_CLIENTS:
            self._fallback_by_key.popitem(last=False)
