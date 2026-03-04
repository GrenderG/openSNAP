"""Shared SNAP protocol enums."""

from enum import IntEnum, IntFlag, auto


class GameTags(IntEnum):
    """Normalized room-flow tag vocabulary used by SNAP room/game handling.

    The PS2 client still sends raw room subcommands such as `0x8001`,
    `0x0658..0x065f`, `0x1468..0x146f`, and `0x8009`; decode those wire values
    into this enum after parsing so transport code can reason about the staged
    flow without conflating local state with the raw subcommand word.

    `CMD_RESULT_WRAPPER` (`0x28`) selector word0 also reuses part of this value
    space. `SLUS_206.42` `kkDispatchingOperation` dispatches:
    - `0x06` to join callbacks (slots 33/34)
    - `0x07` to leave callbacks (slots 35/36)
    """

    SYNC = 0x00
    SYS = 0x01
    SYS2 = 0x02
    SYS_OK = 0x03
    START_OK = 0x04
    READY = 0x05
    GAME_START = 0x06
    GAME_OVER = 0x07
    JOIN_OK = 0x08
    JOIN_NG = 0x09
    PAUSE = 0x0A
    WAIT_OVER = 0x0B
    RESULT = 0x0C
    RESULT2 = 0x0D
    OWNER = 0x0E
    ECHO = 0x0F
    RESET = 0x10
    TIME_OUT = 0x11


class RoomGameState(IntEnum):
    """Tracked server-side phases for one SNAP room game session."""

    INIT = auto()
    SYNC_STARTED = auto()
    IN_GAME = auto()
    GAME_OVER = auto()
    RESULT = auto()


class PostGameReportMask(IntFlag):
    """Tracked post-game report families received from one room member."""

    NONE = 0
    GAME_OVER = 0x01
    RESULT = 0x02
    COMPLETE = GAME_OVER | RESULT


class RoomSubcommand(IntEnum):
    """Known SNAP room subcommands observed in the PS2 room flow."""

    GAME_START = 0x8001
    RESULT2 = 0x8009
    GAME_OVER_MIN = 0x0658
    GAME_OVER_MAX = 0x065F
    RESULT_MIN = 0x1468
    RESULT_MAX = 0x146F
    JOIN_HOST_SYNC = 0x8005
    JOIN_GUEST_SYNC = 0x8102
    JOIN_READY = 0x8008
