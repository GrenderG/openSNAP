"""Game room state management."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class GameRoom:
    """Game room metadata and membership."""

    room_id: int
    name: str
    password: str
    rules: int
    max_players: int
    lobby_id: int
    host_session_id: int
    members: set[int] = field(default_factory=set)


class RoomRegistry:
    """In-memory game room registry."""

    def __init__(self) -> None:
        self._rooms: dict[int, GameRoom] = {}
        self._next_id = 1

    def create_room(
        self,
        *,
        name: str,
        password: str,
        rules: int,
        max_players: int,
        lobby_id: int,
        host_session_id: int,
    ) -> GameRoom:
        """Create a room and add host as first member."""

        room = GameRoom(
            room_id=self._next_id,
            name=name,
            password=password,
            rules=rules,
            max_players=max_players,
            lobby_id=lobby_id,
            host_session_id=host_session_id,
            members={host_session_id},
        )
        self._rooms[room.room_id] = room
        self._next_id += 1
        return room

    def get(self, room_id: int) -> GameRoom | None:
        """Get room by id."""

        return self._rooms.get(room_id)

    def list_for_lobby(self, lobby_id: int) -> list[GameRoom]:
        """List rooms for lobby id."""

        return [room for room in self._rooms.values() if room.lobby_id == lobby_id]

    def join(self, room_id: int, session_id: int) -> bool:
        """Join room if capacity allows."""

        room = self._rooms.get(room_id)
        if room is None:
            return False

        if len(room.members) >= room.max_players and session_id not in room.members:
            return False

        room.members.add(session_id)
        return True

    def leave(self, room_id: int, session_id: int) -> None:
        """Leave room and remove empty rooms."""

        room = self._rooms.get(room_id)
        if room is None:
            return

        room.members.discard(session_id)
        if not room.members:
            self._rooms.pop(room_id, None)
