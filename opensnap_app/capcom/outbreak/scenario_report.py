"""The scenario result report Resident Evil Outbreak uploads after a scenario (APP 6301).

File#1 (`SLUS_207.65` v2, `netwk.bin` `0x0059ba40`) and File#2 (`SLUS_209.84`
main `0x001fd530`, EU `SLES_533.19` `0x001c8b60`) serialise the same report:

    u8 cleared, u8 free mode, u8 scenario, field login, u8 cleared, u8 character,
    u32 clear ticks, u32 points, 3 x (field name, u8 status, u8 character),
    u32 echo time, u32 network error, u32 echo replies, u32 echo losses,
    u32 peak queue, u32 peak location, u32 peak messages, u32 flags

File#1 never writes the last two words (its summary is zeroed, `0x0057bd88`);
File#2 fills them with the peak of a one-second message counter (`0x001f2384`)
and a `game.bin` flag word (`0x00300770`). What each game means by the other
fields is documented next to its storage code (`outbreak/results.py`,
`outbreak2/results.py`).
"""

from dataclasses import dataclass
import struct

from opensnap_app.capcom.protocol import decode_app_field

TEAMMATE_SLOTS = 3
TEAMMATE_ALIVE = 1
TRAILING_WORDS = 8


@dataclass(frozen=True, slots=True)
class Teammate:
    name: str
    alive: bool
    character: int


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """One decoded 6301 report."""

    scenario: int
    # The report's second byte: File#1's free-mode flag, File#2's rank kind.
    mode: int
    cleared: bool
    character: int
    clear_ticks: int
    points: int
    teammates: tuple[Teammate, ...]
    echo_ticks: int
    echo_replies: int
    echo_losses: int
    network_error: int
    peak_queue: int
    peak_location: int
    peak_messages: int = 0
    flags: int = 0


def decode_scenario_result(payload: bytes, sequence: int) -> ScenarioResult:
    """Decode one 6301 request payload."""

    if len(payload) < 3:
        raise ValueError('Scenario result report is truncated.')
    cleared, mode, scenario = payload[:3]
    _, size = decode_app_field(payload[3:], sequence)
    offset = 3 + size
    if len(payload) < offset + 10:
        raise ValueError('Scenario result report is truncated.')
    character = payload[offset + 1]
    clear_ticks, points = struct.unpack_from('>LL', payload, offset + 2)
    offset += 10
    teammates = []
    for _ in range(TEAMMATE_SLOTS):
        name, size = decode_app_field(payload[offset:], sequence)
        offset += size
        if len(payload) < offset + 2:
            raise ValueError('Scenario result report is truncated.')
        status, mate_character = payload[offset:offset + 2]
        offset += 2
        if name := _text(name):
            teammates.append(Teammate(name, status == TEAMMATE_ALIVE, mate_character))
    if len(payload) != offset + 4 * TRAILING_WORDS:
        raise ValueError('Scenario result report has an invalid size.')
    (
        echo_ticks, network_error, echo_replies, echo_losses, peak_queue, peak_location, peak_messages, flags,
    ) = struct.unpack_from(f'>{TRAILING_WORDS}L', payload, offset)
    return ScenarioResult(
        scenario=scenario,
        mode=mode,
        cleared=bool(cleared),
        character=character,
        clear_ticks=clear_ticks,
        points=points,
        teammates=tuple(teammates),
        echo_ticks=echo_ticks,
        echo_replies=echo_replies,
        echo_losses=echo_losses,
        network_error=network_error,
        peak_queue=peak_queue,
        peak_location=peak_location,
        peak_messages=peak_messages,
        flags=flags,
    )


def _text(field: bytes) -> str:
    return field.split(b'\x00', 1)[0].decode('ascii', errors='replace')
