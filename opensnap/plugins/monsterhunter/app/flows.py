"""APP connection phase tracking shared by the SNAP handlers and the APP service."""

from enum import StrEnum
import threading


class AppPhase(StrEnum):
    """Native APP connection kinds, in client order."""

    APP1 = 'APP1'
    WORLD = 'WORLD'
    POSTWORLD = 'POSTWORLD'
    LAND = 'LAND'


class AppFlowTracker:
    """Classify APP TCP connections, which carry no SNAP session id.

    APP1 runs before SNAP login. After KICS login a session owns its own
    WORLD -> POSTWORLD -> LAND progression; connections from one host are
    attributed to that host's pending sessions in login order. Later LAND
    reconnects that cannot be attributed use the host's last released phase.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase_by_session: dict[int, AppPhase] = {}
        self._host_by_session: dict[int, str] = {}
        self._pending_by_host: dict[str, list[int]] = {}
        self._active_by_host: dict[str, int] = {}
        self._fallback_by_host: dict[str, AppPhase] = {}

    def arm_world(self, session_id: int, host: str) -> None:
        """Start the online APP flow for one logged-in session without rewinding it."""

        with self._lock:
            self._host_by_session[session_id] = host
            self._phase_by_session.setdefault(session_id, AppPhase.WORLD)
            pending = self._pending_by_host.setdefault(host, [])
            if self._active_by_host.get(host) != session_id and session_id not in pending:
                pending.append(session_id)

    def forget(self, session_id: int) -> None:
        """Drop one logged-out session; a host with no sessions restarts at APP1."""

        with self._lock:
            host = self._host_by_session.pop(session_id, None)
            self._phase_by_session.pop(session_id, None)
            if host is None:
                return
            if self._active_by_host.get(host) == session_id:
                self._active_by_host.pop(host, None)
            pending = self._pending_by_host.get(host, [])
            if session_id in pending:
                pending.remove(session_id)
            if host not in self._host_by_session.values():
                self._pending_by_host.pop(host, None)
                self._fallback_by_host.pop(host, None)

    def claim(self, host: str) -> tuple[int | None, AppPhase]:
        """Return the owner session and phase for one new APP connection."""

        with self._lock:
            active = self._active_by_host.get(host)
            if active is not None and active in self._phase_by_session:
                return active, self._phase_by_session[active]
            self._active_by_host.pop(host, None)

            pending = self._pending_by_host.get(host, [])
            while pending:
                session_id = pending.pop(0)
                if session_id in self._phase_by_session:
                    self._active_by_host[host] = session_id
                    return session_id, self._phase_by_session[session_id]
            return None, self._fallback_by_host.get(host, AppPhase.APP1)

    def advance(self, host: str, session_id: int | None, phase: AppPhase) -> None:
        """Record the phase the next connection of this flow will use."""

        with self._lock:
            if session_id is None:
                self._fallback_by_host[host] = phase
            elif session_id in self._phase_by_session:
                self._phase_by_session[session_id] = phase

    def attribute(self, host: str, owner: int | None) -> int | None:
        """Return the session an APP request belongs to, or None if ambiguous.

        Unattributed connections (later LAND reconnects) belong to the host's
        only logged-in session; with several sessions behind one address the
        player cannot be told apart.
        """

        if owner is not None:
            return owner
        with self._lock:
            sessions = [session_id for session_id, address in self._host_by_session.items() if address == host]
        return sessions[0] if len(sessions) == 1 else None

    def release(self, host: str, session_id: int | None) -> None:
        """Finish one LAND connection; later unattributed reconnects use LAND."""

        with self._lock:
            if session_id is not None:
                if session_id in self._phase_by_session:
                    self._phase_by_session[session_id] = AppPhase.LAND
                if self._active_by_host.get(host) == session_id:
                    self._active_by_host.pop(host, None)
            self._fallback_by_host[host] = AppPhase.LAND
